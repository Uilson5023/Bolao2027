"""
Bolão Copa do Mundo 2026
=========================
Sistema web para gerenciar um bolão de apostas esportivas, baseado na
planilha "Bolão Copa do Mundo 26".

Funcionalidades:
- Cadastro/login de apostadores
- Inserção de palpites (placar) para cada um dos 72 jogos da fase de grupos
- Lançamento dos resultados reais (área do organizador/admin)
- Cálculo automático da pontuação de cada palpite, segundo as regras oficiais
- Ranking geral atualizado automaticamente sempre que um resultado é lançado

Como executar:
    pip install flask
    python app.py
Depois acesse http://localhost:5000 no navegador.

Banco de dados: SQLite (arquivo bolao.db, criado automaticamente).
"""

import json
import os
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, g, redirect, render_template, request, session,
                    url_for, flash)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bolao.db")
GAMES_JSON = os.path.join(BASE_DIR, "games.json")

app = Flask(__name__)
app.config["SECRET_KEY"] = "troque-esta-chave-em-producao"

# Pontuação oficial (conforme aba "Regras" da planilha)
PTS_PLACAR_EXATO = 10        # acertou o placar exato
PTS_VENCEDOR_GOLS = 6        # acertou vencedor + nº de gols de uma das seleções
PTS_SOMENTE_VENCEDOR = 4     # acertou somente o vencedor
PTS_EMPATE_NAO_EXATO = 4     # acertou que seria empate, mas não o placar exato
PTS_GOLS_UMA_SELECAO = 1     # acertou o nº de gols de uma das seleções (mais nada)
PTS_ERROU = 0

# Tamanho de cada rodada (para exibição em carrossel)
JOGOS_POR_RODADA = 10


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            criado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS campeonatos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 0,
            criado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jogos (
            id INTEGER PRIMARY KEY,            -- número do jogo (global, autoincrementa)
            campeonato_id INTEGER REFERENCES campeonatos(id) ON DELETE CASCADE,
            time_a TEXT NOT NULL,
            time_b TEXT NOT NULL,
            gols_a INTEGER,                    -- resultado real (NULL = não jogado)
            gols_b INTEGER
        );

        CREATE TABLE IF NOT EXISTS palpites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            jogo_id INTEGER NOT NULL REFERENCES jogos(id) ON DELETE CASCADE,
            gols_a INTEGER NOT NULL,
            gols_b INTEGER NOT NULL,
            pontos INTEGER NOT NULL DEFAULT 0,
            UNIQUE(usuario_id, jogo_id)
        );
        """
    )
    db.commit()

    # Migração: bancos criados antes da tabela "campeonatos" existir não têm a
    # coluna jogos.campeonato_id. Adiciona a coluna se necessário.
    colunas_jogos = [row["name"] for row in db.execute("PRAGMA table_info(jogos)")]
    if "campeonato_id" not in colunas_jogos:
        db.execute(
            "ALTER TABLE jogos ADD COLUMN campeonato_id INTEGER REFERENCES campeonatos(id) ON DELETE CASCADE"
        )
        db.commit()

    # Garante que exista pelo menos um campeonato. Se não houver nenhum,
    # cria o campeonato padrão "Copa do Mundo 2026" e o marca como ativo.
    cur = db.execute("SELECT COUNT(*) AS c FROM campeonatos")
    if cur.fetchone()["c"] == 0:
        db.execute(
            "INSERT INTO campeonatos (nome, ativo, criado_em) VALUES (?,1,?)",
            ("Copa do Mundo 2026", datetime.utcnow().isoformat()),
        )
        db.commit()

    campeonato_padrao_id = db.execute(
        "SELECT id FROM campeonatos ORDER BY id LIMIT 1"
    ).fetchone()["id"]

    # Carrega os 72 jogos a partir de games.json (apenas na primeira vez)
    cur = db.execute("SELECT COUNT(*) AS c FROM jogos")
    if cur.fetchone()["c"] == 0:
        with open(GAMES_JSON, "r", encoding="utf-8") as f:
            games = json.load(f)
        for game in games:
            db.execute(
                "INSERT INTO jogos (id, campeonato_id, time_a, time_b, gols_a, gols_b) VALUES (?,?,?,?,?,?)",
                (
                    game["num"],
                    campeonato_padrao_id,
                    game["team_a"],
                    game["team_b"],
                    int(game["score_a"]) if game["score_a"] not in (None, "") else None,
                    int(game["score_b"]) if game["score_b"] not in (None, "") else None,
                ),
            )
        db.commit()

    # Jogos "órfãos" (de bancos antigos, sem campeonato_id) são atribuídos
    # ao campeonato padrão para não sumirem da aplicação.
    db.execute(
        "UPDATE jogos SET campeonato_id=? WHERE campeonato_id IS NULL",
        (campeonato_padrao_id,),
    )
    db.commit()

    # Cria um usuário administrador padrão, se ainda não existir nenhum admin
    cur = db.execute("SELECT COUNT(*) AS c FROM usuarios WHERE is_admin = 1")
    if cur.fetchone()["c"] == 0:
        db.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, is_admin, criado_em) VALUES (?,?,?,?,?)",
            (
                "Organizador",
                "admin@bolao.com",
                generate_password_hash("admin123"),
                1,
                datetime.utcnow().isoformat(),
            ),
        )
        db.commit()
    db.close()


def get_campeonato_ativo(db):
    """Retorna a linha do campeonato atualmente ativo, ou None se não houver nenhum."""
    return db.execute("SELECT * FROM campeonatos WHERE ativo = 1 LIMIT 1").fetchone()


# ---------------------------------------------------------------------------
# Regras de pontuação
# ---------------------------------------------------------------------------

def calcular_pontos(palpite_a, palpite_b, real_a, real_b):
    """Calcula os pontos de um palpite, dado o resultado real do jogo.

    Regras (aba "Regras" da planilha original):
      10 pts -> placar exato
       6 pts -> acertou o vencedor (ou empate) E o nº de gols de uma das seleções
       4 pts -> acertou somente o vencedor (sem acertar nenhum placar)
       4 pts -> acertou que haveria empate, mas não acertou o placar exato
       1 pt  -> acertou o nº de gols de apenas uma das seleções (mais nada)
       0 pts -> não acertou nada
    """
    if real_a is None or real_b is None:
        return None  # jogo ainda não realizado

    # 1) Placar exato
    if palpite_a == real_a and palpite_b == real_b:
        return PTS_PLACAR_EXATO

    resultado_real_empate = real_a == real_b
    resultado_palpite_empate = palpite_a == palpite_b

    # vencedor real (None se empate)
    vencedor_real = None
    if not resultado_real_empate:
        vencedor_real = "A" if real_a > real_b else "B"

    vencedor_palpite = None
    if not resultado_palpite_empate:
        vencedor_palpite = "A" if palpite_a > palpite_b else "B"

    acertou_vencedor_ou_empate = (
        (resultado_real_empate and resultado_palpite_empate)
        or (not resultado_real_empate and vencedor_real == vencedor_palpite)
    )

    acertou_gols_a = palpite_a == real_a
    acertou_gols_b = palpite_b == real_b
    acertou_alguma_selecao = acertou_gols_a or acertou_gols_b

    # 2) Empate (não exato): acertou que seria empate mas não o placar
    if resultado_real_empate and resultado_palpite_empate:
        return PTS_EMPATE_NAO_EXATO

    # 3) Vencedor + nº de gols de uma das seleções
    if acertou_vencedor_ou_empate and acertou_alguma_selecao:
        return PTS_VENCEDOR_GOLS

    # 4) Somente o vencedor
    if acertou_vencedor_ou_empate:
        return PTS_SOMENTE_VENCEDOR

    # 5) Número de gols de uma das seleções (sem acertar vencedor)
    if acertou_alguma_selecao:
        return PTS_GOLS_UMA_SELECAO

    return PTS_ERROU


def recalcular_pontos_jogo(db, jogo_id):
    """Recalcula a pontuação de todos os palpites referentes a um jogo."""
    jogo = db.execute("SELECT * FROM jogos WHERE id=?", (jogo_id,)).fetchone()
    if jogo is None:
        return
    palpites = db.execute("SELECT * FROM palpites WHERE jogo_id=?", (jogo_id,)).fetchall()
    for p in palpites:
        pontos = calcular_pontos(p["gols_a"], p["gols_b"], jogo["gols_a"], jogo["gols_b"])
        db.execute(
            "UPDATE palpites SET pontos=? WHERE id=?",
            (pontos if pontos is not None else 0, p["id"]),
        )
    db.commit()


# ---------------------------------------------------------------------------
# Rodadas (agrupamento dos 72 jogos em blocos de 10, para o carrossel)
# ---------------------------------------------------------------------------

def montar_rodadas(jogos, por_rodada=JOGOS_POR_RODADA):
    """Agrupa uma lista de jogos (ordenada por id) em rodadas de N jogos.

    Retorna uma lista de dicts: {"numero": int, "jogos": [...]}
    """
    rodadas = []
    for i in range(0, len(jogos), por_rodada):
        bloco = list(jogos[i:i + por_rodada])
        rodadas.append({
            "numero": (i // por_rodada) + 1,
            "jogos": bloco,
        })
    return rodadas


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("usuario_id"):
            flash("Você precisa fazer login para continuar.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Acesso restrito ao organizador do bolão.", "danger")
            return redirect(url_for("ranking"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    return {
        "usuario_logado": session.get("usuario_nome"),
        "is_admin": session.get("is_admin", False),
    }


# ---------------------------------------------------------------------------
# Rotas: Autenticação
# ---------------------------------------------------------------------------

@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        confirmar = request.form.get("confirmar_senha", "")

        if not nome or not email or not senha:
            flash("Preencha todos os campos.", "danger")
            return render_template("cadastro.html")
        if senha != confirmar:
            flash("As senhas não coincidem.", "danger")
            return render_template("cadastro.html")
        if len(senha) < 4:
            flash("A senha deve ter ao menos 4 caracteres.", "danger")
            return render_template("cadastro.html")

        db = get_db()
        existente = db.execute("SELECT id FROM usuarios WHERE email=?", (email,)).fetchone()
        if existente:
            flash("Já existe um cadastro com este e-mail.", "danger")
            return render_template("cadastro.html")

        db.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, is_admin, criado_em) VALUES (?,?,?,0,?)",
            (nome, email, generate_password_hash(senha), datetime.utcnow().isoformat()),
        )
        db.commit()
        flash("Cadastro realizado com sucesso! Faça login para inserir seus palpites.", "success")
        return redirect(url_for("login"))

    return render_template("cadastro.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        db = get_db()
        usuario = db.execute("SELECT * FROM usuarios WHERE email=?", (email,)).fetchone()
        if usuario and check_password_hash(usuario["senha_hash"], senha):
            session["usuario_id"] = usuario["id"]
            session["usuario_nome"] = usuario["nome"]
            session["is_admin"] = bool(usuario["is_admin"])
            flash(f"Bem-vindo(a), {usuario['nome']}!", "success")
            return redirect(url_for("palpites"))
        flash("E-mail ou senha incorretos.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Você saiu da sua conta.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Rotas: Palpites
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("ranking"))


@app.route("/palpites", methods=["GET", "POST"])
@login_required
def palpites():
    db = get_db()
    usuario_id = session["usuario_id"]

    campeonato = get_campeonato_ativo(db)
    if campeonato is None:
        flash("Não há nenhum campeonato ativo no momento. Fale com o organizador.", "warning")
        return render_template("palpites.html", jogos=[], meus_palpites={}, rodadas=[], campeonato=None)

    if request.method == "POST":
        jogos = db.execute(
            "SELECT * FROM jogos WHERE campeonato_id=? ORDER BY id", (campeonato["id"],)
        ).fetchall()
        atualizados = 0
        for jogo in jogos:
            # só permite inserir/editar palpite se o jogo ainda não começou
            # (aqui simplificamos: só bloqueia quando já existe resultado lançado)
            if jogo["gols_a"] is not None:
                continue
            campo_a = request.form.get(f"gols_a_{jogo['id']}")
            campo_b = request.form.get(f"gols_b_{jogo['id']}")
            if campo_a is None or campo_b is None or campo_a == "" or campo_b == "":
                continue
            try:
                ga, gb = int(campo_a), int(campo_b)
                if ga < 0 or gb < 0:
                    continue
            except ValueError:
                continue

            existente = db.execute(
                "SELECT id FROM palpites WHERE usuario_id=? AND jogo_id=?",
                (usuario_id, jogo["id"]),
            ).fetchone()
            if existente:
                db.execute(
                    "UPDATE palpites SET gols_a=?, gols_b=? WHERE id=?",
                    (ga, gb, existente["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO palpites (usuario_id, jogo_id, gols_a, gols_b, pontos) VALUES (?,?,?,?,0)",
                    (usuario_id, jogo["id"], ga, gb),
                )
            atualizados += 1
        db.commit()
        flash(f"{atualizados} palpite(s) salvo(s) com sucesso!", "success")
        return redirect(url_for("palpites"))

    jogos = db.execute(
        "SELECT * FROM jogos WHERE campeonato_id=? ORDER BY id", (campeonato["id"],)
    ).fetchall()
    meus_palpites = {
        row["jogo_id"]: row
        for row in db.execute(
            "SELECT * FROM palpites WHERE usuario_id=?", (usuario_id,)
        ).fetchall()
    }

    rodadas = montar_rodadas(jogos)
    for rodada in rodadas:
        rodada["preenchidos"] = sum(
            1 for j in rodada["jogos"] if j["id"] in meus_palpites
        )

    return render_template(
        "palpites.html",
        jogos=jogos,
        meus_palpites=meus_palpites,
        rodadas=rodadas,
        campeonato=campeonato,
    )


# ---------------------------------------------------------------------------
# Rotas: Ranking
# ---------------------------------------------------------------------------

@app.route("/ranking")
def ranking():
    db = get_db()

    campeonatos = db.execute("SELECT * FROM campeonatos ORDER BY criado_em DESC").fetchall()

    campeonato_id_param = request.args.get("campeonato_id", type=int)
    if campeonato_id_param is not None:
        campeonato = db.execute(
            "SELECT * FROM campeonatos WHERE id=?", (campeonato_id_param,)
        ).fetchone()
    else:
        campeonato = get_campeonato_ativo(db)
        if campeonato is None and campeonatos:
            campeonato = campeonatos[0]

    if campeonato is None:
        return render_template(
            "ranking.html",
            ranking=[],
            jogos_com_resultado=0,
            total_jogos=0,
            campeonatos=campeonatos,
            campeonato=None,
        )

    linhas = db.execute(
        """
        SELECT u.id, u.nome,
               COALESCE(SUM(p.pontos), 0) AS total_pontos,
               SUM(CASE WHEN p.pontos = ? THEN 1 ELSE 0 END) AS qt_10,
               SUM(CASE WHEN p.pontos = ? THEN 1 ELSE 0 END) AS qt_6,
               SUM(CASE WHEN p.pontos = ? THEN 1 ELSE 0 END) AS qt_4,
               SUM(CASE WHEN p.pontos = ? THEN 1 ELSE 0 END) AS qt_1,
               COUNT(p.id) AS qt_palpites
        FROM usuarios u
        LEFT JOIN palpites p ON p.usuario_id = u.id
             AND p.jogo_id IN (SELECT id FROM jogos WHERE campeonato_id = ?)
        WHERE u.is_admin = 0
        GROUP BY u.id, u.nome
        ORDER BY total_pontos DESC, qt_10 DESC, qt_6 DESC, qt_4 DESC, u.nome ASC
        """,
        (PTS_PLACAR_EXATO, PTS_VENCEDOR_GOLS, PTS_SOMENTE_VENCEDOR, PTS_GOLS_UMA_SELECAO, campeonato["id"]),
    ).fetchall()

    jogos_com_resultado = db.execute(
        "SELECT COUNT(*) AS c FROM jogos WHERE campeonato_id=? AND gols_a IS NOT NULL",
        (campeonato["id"],),
    ).fetchone()["c"]
    total_jogos = db.execute(
        "SELECT COUNT(*) AS c FROM jogos WHERE campeonato_id=?", (campeonato["id"],)
    ).fetchone()["c"]

    return render_template(
        "ranking.html",
        ranking=linhas,
        jogos_com_resultado=jogos_com_resultado,
        total_jogos=total_jogos,
        campeonatos=campeonatos,
        campeonato=campeonato,
    )


# ---------------------------------------------------------------------------
# Rotas: Administração (lançar resultados)
# ---------------------------------------------------------------------------

@app.route("/admin/resultados", methods=["GET", "POST"])
@login_required
@admin_required
def admin_resultados():
    db = get_db()
    campeonato = get_campeonato_ativo(db)

    if request.method == "POST":
        if campeonato is None:
            flash("Ative um campeonato antes de lançar resultados.", "danger")
            return redirect(url_for("admin_campeonatos"))
        jogo_id = int(request.form["jogo_id"])
        gols_a = request.form.get("gols_a", "").strip()
        gols_b = request.form.get("gols_b", "").strip()
        if gols_a == "" or gols_b == "":
            db.execute("UPDATE jogos SET gols_a=NULL, gols_b=NULL WHERE id=?", (jogo_id,))
        else:
            db.execute(
                "UPDATE jogos SET gols_a=?, gols_b=? WHERE id=?",
                (int(gols_a), int(gols_b), jogo_id),
            )
        db.commit()
        recalcular_pontos_jogo(db, jogo_id)
        flash(f"Resultado do jogo {jogo_id} atualizado e pontuações recalculadas!", "success")
        return redirect(url_for("admin_resultados"))

    if campeonato is None:
        flash("Nenhum campeonato ativo. Crie ou ative um campeonato para lançar resultados.", "warning")
        return render_template("admin_resultados.html", jogos=[], rodadas=[], campeonato=None)

    jogos = db.execute(
        "SELECT * FROM jogos WHERE campeonato_id=? ORDER BY id", (campeonato["id"],)
    ).fetchall()

    rodadas = montar_rodadas(jogos)
    for rodada in rodadas:
        rodada["lancados"] = sum(
            1 for j in rodada["jogos"] if j["gols_a"] is not None
        )

    return render_template("admin_resultados.html", jogos=jogos, rodadas=rodadas, campeonato=campeonato)


@app.route("/admin/resultados/<int:jogo_id>/excluir", methods=["POST"])
@login_required
@admin_required
def admin_excluir_resultado(jogo_id):
    db = get_db()
    jogo = db.execute("SELECT * FROM jogos WHERE id=?", (jogo_id,)).fetchone()
    if jogo is None:
        flash("Jogo não encontrado.", "danger")
        return redirect(url_for("admin_resultados"))
    db.execute("UPDATE jogos SET gols_a=NULL, gols_b=NULL WHERE id=?", (jogo_id,))
    db.commit()
    recalcular_pontos_jogo(db, jogo_id)
    flash(
        f"Resultado do jogo {jogo_id} ({jogo['time_a']} x {jogo['time_b']}) foi excluído. "
        "As pontuações desse jogo foram zeradas.",
        "success",
    )
    return redirect(url_for("admin_resultados"))


@app.route("/admin/recalcular-tudo")
@login_required
@admin_required
def admin_recalcular_tudo():
    db = get_db()
    jogos = db.execute("SELECT id FROM jogos").fetchall()
    for j in jogos:
        recalcular_pontos_jogo(db, j["id"])
    flash("Todas as pontuações foram recalculadas!", "success")
    return redirect(url_for("admin_resultados"))


# ---------------------------------------------------------------------------
# Rotas: Administração (campeonatos)
# ---------------------------------------------------------------------------

@app.route("/admin/campeonatos", methods=["GET", "POST"])
@login_required
@admin_required
def admin_campeonatos():
    db = get_db()

    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        if not nome:
            flash("Informe um nome para o campeonato.", "danger")
            return redirect(url_for("admin_campeonatos"))

        ativar_imediatamente = request.form.get("ativar") == "on"
        if ativar_imediatamente:
            db.execute("UPDATE campeonatos SET ativo=0")

        db.execute(
            "INSERT INTO campeonatos (nome, ativo, criado_em) VALUES (?,?,?)",
            (nome, 1 if ativar_imediatamente else 0, datetime.utcnow().isoformat()),
        )
        db.commit()
        flash(f"Campeonato \"{nome}\" criado com sucesso!", "success")
        return redirect(url_for("admin_campeonatos"))

    campeonatos = db.execute(
        """
        SELECT c.*,
               COUNT(j.id) AS qt_jogos,
               SUM(CASE WHEN j.gols_a IS NOT NULL THEN 1 ELSE 0 END) AS qt_finalizados
        FROM campeonatos c
        LEFT JOIN jogos j ON j.campeonato_id = c.id
        GROUP BY c.id
        ORDER BY c.criado_em DESC
        """
    ).fetchall()

    return render_template("admin_campeonatos.html", campeonatos=campeonatos)


@app.route("/admin/campeonatos/<int:campeonato_id>/ativar", methods=["POST"])
@login_required
@admin_required
def admin_ativar_campeonato(campeonato_id):
    db = get_db()
    campeonato = db.execute("SELECT * FROM campeonatos WHERE id=?", (campeonato_id,)).fetchone()
    if campeonato is None:
        flash("Campeonato não encontrado.", "danger")
        return redirect(url_for("admin_campeonatos"))
    db.execute("UPDATE campeonatos SET ativo=0")
    db.execute("UPDATE campeonatos SET ativo=1 WHERE id=?", (campeonato_id,))
    db.commit()
    flash(f"Campeonato \"{campeonato['nome']}\" agora está ativo para apostas e ranking.", "success")
    return redirect(url_for("admin_campeonatos"))


@app.route("/admin/campeonatos/<int:campeonato_id>/excluir", methods=["POST"])
@login_required
@admin_required
def admin_excluir_campeonato(campeonato_id):
    db = get_db()
    campeonato = db.execute("SELECT * FROM campeonatos WHERE id=?", (campeonato_id,)).fetchone()
    if campeonato is None:
        flash("Campeonato não encontrado.", "danger")
        return redirect(url_for("admin_campeonatos"))
    # Apaga explicitamente os jogos do campeonato primeiro (isso já cascateia
    # para os palpites, cuja FK sempre teve ON DELETE CASCADE). Evita depender
    # de bancos antigos onde a FK jogos->campeonatos foi adicionada via ALTER TABLE.
    db.execute("DELETE FROM jogos WHERE campeonato_id=?", (campeonato_id,))
    db.execute("DELETE FROM campeonatos WHERE id=?", (campeonato_id,))
    db.commit()
    flash(
        f"Campeonato \"{campeonato['nome']}\" excluído, junto com suas partidas e palpites relacionados.",
        "success",
    )
    return redirect(url_for("admin_campeonatos"))


# ---------------------------------------------------------------------------
# Rotas: Administração (remodelar partidas)
# ---------------------------------------------------------------------------

@app.route("/admin/partidas")
@login_required
@admin_required
def admin_partidas():
    db = get_db()

    campeonato_id = request.args.get("campeonato_id", type=int)
    if campeonato_id is None:
        ativo = get_campeonato_ativo(db)
        campeonato_id = ativo["id"] if ativo else None

    campeonatos = db.execute("SELECT * FROM campeonatos ORDER BY criado_em DESC").fetchall()

    campeonato = None
    jogos = []
    if campeonato_id is not None:
        campeonato = db.execute("SELECT * FROM campeonatos WHERE id=?", (campeonato_id,)).fetchone()
        if campeonato is not None:
            jogos = db.execute(
                "SELECT * FROM jogos WHERE campeonato_id=? ORDER BY id", (campeonato_id,)
            ).fetchall()

    return render_template(
        "admin_partidas.html",
        campeonatos=campeonatos,
        campeonato=campeonato,
        jogos=jogos,
    )


@app.route("/admin/partidas/adicionar", methods=["POST"])
@login_required
@admin_required
def admin_adicionar_partida():
    db = get_db()
    campeonato_id = request.form.get("campeonato_id", type=int)
    time_a = request.form.get("time_a", "").strip()
    time_b = request.form.get("time_b", "").strip()

    campeonato = db.execute("SELECT * FROM campeonatos WHERE id=?", (campeonato_id,)).fetchone()
    if campeonato is None:
        flash("Selecione um campeonato válido.", "danger")
        return redirect(url_for("admin_partidas"))

    if not time_a or not time_b:
        flash("Informe os dois times da partida.", "danger")
        return redirect(url_for("admin_partidas", campeonato_id=campeonato_id))

    db.execute(
        "INSERT INTO jogos (campeonato_id, time_a, time_b, gols_a, gols_b) VALUES (?,?,?,NULL,NULL)",
        (campeonato_id, time_a, time_b),
    )
    db.commit()
    flash(f"Partida \"{time_a} x {time_b}\" adicionada ao campeonato \"{campeonato['nome']}\".", "success")
    return redirect(url_for("admin_partidas", campeonato_id=campeonato_id))


@app.route("/admin/partidas/<int:jogo_id>/editar", methods=["POST"])
@login_required
@admin_required
def admin_editar_partida(jogo_id):
    db = get_db()
    jogo = db.execute("SELECT * FROM jogos WHERE id=?", (jogo_id,)).fetchone()
    if jogo is None:
        flash("Partida não encontrada.", "danger")
        return redirect(url_for("admin_partidas"))

    time_a = request.form.get("time_a", "").strip()
    time_b = request.form.get("time_b", "").strip()
    if not time_a or not time_b:
        flash("Informe os dois times da partida.", "danger")
        return redirect(url_for("admin_partidas", campeonato_id=jogo["campeonato_id"]))

    db.execute("UPDATE jogos SET time_a=?, time_b=? WHERE id=?", (time_a, time_b, jogo_id))
    db.commit()
    flash(f"Partida {jogo_id} atualizada para \"{time_a} x {time_b}\".", "success")
    return redirect(url_for("admin_partidas", campeonato_id=jogo["campeonato_id"]))


@app.route("/admin/partidas/<int:jogo_id>/excluir", methods=["POST"])
@login_required
@admin_required
def admin_excluir_partida(jogo_id):
    db = get_db()
    jogo = db.execute("SELECT * FROM jogos WHERE id=?", (jogo_id,)).fetchone()
    if jogo is None:
        flash("Partida não encontrada.", "danger")
        return redirect(url_for("admin_partidas"))
    campeonato_id = jogo["campeonato_id"]
    db.execute("DELETE FROM jogos WHERE id=?", (jogo_id,))
    db.commit()
    flash(
        f"Partida \"{jogo['time_a']} x {jogo['time_b']}\" removida, junto com os palpites relacionados a ela.",
        "success",
    )
    return redirect(url_for("admin_partidas", campeonato_id=campeonato_id))

init_db()
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
