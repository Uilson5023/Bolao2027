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

        CREATE TABLE IF NOT EXISTS jogos (
            id INTEGER PRIMARY KEY,            -- número do jogo (1..72)
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

    # Carrega os 72 jogos a partir de games.json (apenas na primeira vez)
    cur = db.execute("SELECT COUNT(*) AS c FROM jogos")
    if cur.fetchone()["c"] == 0:
        with open(GAMES_JSON, "r", encoding="utf-8") as f:
            games = json.load(f)
        for game in games:
            db.execute(
                "INSERT INTO jogos (id, time_a, time_b, gols_a, gols_b) VALUES (?,?,?,?,?)",
                (
                    game["num"],
                    game["team_a"],
                    game["team_b"],
                    int(game["score_a"]) if game["score_a"] not in (None, "") else None,
                    int(game["score_b"]) if game["score_b"] not in (None, "") else None,
                ),
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

    if request.method == "POST":
        jogos = db.execute("SELECT * FROM jogos ORDER BY id").fetchall()
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

    jogos = db.execute("SELECT * FROM jogos ORDER BY id").fetchall()
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
    )


# ---------------------------------------------------------------------------
# Rotas: Ranking
# ---------------------------------------------------------------------------

@app.route("/ranking")
def ranking():
    db = get_db()
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
        WHERE u.is_admin = 0
        GROUP BY u.id, u.nome
        ORDER BY total_pontos DESC, qt_10 DESC, qt_6 DESC, qt_4 DESC, u.nome ASC
        """,
        (PTS_PLACAR_EXATO, PTS_VENCEDOR_GOLS, PTS_SOMENTE_VENCEDOR, PTS_GOLS_UMA_SELECAO),
    ).fetchall()

    jogos_com_resultado = db.execute(
        "SELECT COUNT(*) AS c FROM jogos WHERE gols_a IS NOT NULL"
    ).fetchone()["c"]
    total_jogos = db.execute("SELECT COUNT(*) AS c FROM jogos").fetchone()["c"]

    return render_template(
        "ranking.html",
        ranking=linhas,
        jogos_com_resultado=jogos_com_resultado,
        total_jogos=total_jogos,
    )


# ---------------------------------------------------------------------------
# Rotas: Administração (lançar resultados)
# ---------------------------------------------------------------------------

@app.route("/admin/resultados", methods=["GET", "POST"])
@login_required
@admin_required
def admin_resultados():
    db = get_db()
    if request.method == "POST":
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

    jogos = db.execute("SELECT * FROM jogos ORDER BY id").fetchall()

    rodadas = montar_rodadas(jogos)
    for rodada in rodadas:
        rodada["lancados"] = sum(
            1 for j in rodada["jogos"] if j["gols_a"] is not None
        )

    return render_template("admin_resultados.html", jogos=jogos, rodadas=rodadas)


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

init_db()
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
