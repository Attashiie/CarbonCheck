"""CarbonCheck - API REST (Flask). ISO 14064-1 (quantificação) e 14064-3 (verificação)."""
import configparser, io, json, math, os, sqlite3, uuid, textwrap, datetime as dt
from contextlib import contextmanager
from functools import wraps
import jwt
import pymysql
from pymysql.cursors import DictCursor
from flask import Flask, jsonify, request, send_file, send_from_directory, g
from click import Choice, ClickException, prompt
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder="static")
APP_ENV = os.environ.get("APP_ENV", "development").lower()
SECRET = os.environ.get("CARBONCHECK_SECRET_KEY")
if APP_ENV == "production" and not SECRET:
    raise RuntimeError("Defina CARBONCHECK_SECRET_KEY no ambiente de produção.")
if APP_ENV == "production" and len(SECRET) < 32:
    raise RuntimeError("CARBONCHECK_SECRET_KEY deve ter pelo menos 32 caracteres.")
SECRET = SECRET or "chave-insegura-apenas-para-desenvolvimento"
DB_PATH = os.environ.get("CARBONCHECK_DB", os.path.join(app.instance_path, "carboncheck.sqlite3"))
MYSQL_CNF = os.environ.get(
    "CARBONCHECK_MYSQL_CNF",
    os.path.expanduser("~/.config/carboncheck/mysql.cnf"),
)
DB_BACKEND = os.environ.get(
    "CARBONCHECK_DB_BACKEND",
    "mysql" if os.path.isfile(MYSQL_CNF) else "sqlite",
).lower()
CATEGORIAS = {1: "Cat. 1 - Emissões diretas", 2: "Cat. 2 - Energia importada", 3: "Cat. 3 - Transporte", 4: "Cat. 4 - Produtos e resíduos"}
STATUS = {"rascunho": "Rascunho", "submetido": "Submetido", "ajuste_solicitado": "Ajuste solicitado", "aprovado": "Aprovado", "reprovado": "Reprovado"}
EDITAVEL = ("rascunho", "ajuste_solicitado")

def F(nome, un, fator, cat): return {"nome": nome, "unidade": un, "fator": fator, "cat": cat}
# kgCO2e por unidade - valores SIMPLIFICADOS/ilustrativos
FATORES = {
    "energia": F("Energia elétrica (rede)", "kWh", 0.0385, 2), "gasolina": F("Gasolina", "L", 2.2, 1),
    "diesel": F("Diesel", "L", 2.6, 1), "gas_natural": F("Gás natural", "m³", 2.0, 1), "glp": F("GLP", "kg", 3.0, 1),
    "carro": F("Transporte rodoviário (carro)", "km", 0.17, 3), "onibus": F("Transporte rodoviário (ônibus)", "km", 0.09, 3),
    "aviao": F("Viagem aérea", "km", 0.15, 3), "residuos": F("Resíduos em aterro", "kg", 0.45, 4),
    "etanol": F("Etanol hidratado", "L", 1.5, 1), "gnv": F("GNV veicular", "m³", 2.0, 1),
    "biodiesel": F("Biodiesel B100 (fator ilustrativo)", "L", 0.45, 1),
    "refrigerante_r134a": F("Vazamento de refrigerante R-134a", "kg", 1430.0, 1),
    "refrigerante_r404a": F("Vazamento de refrigerante R-404A", "kg", 3922.0, 1),
    "refrigerante_r410a": F("Vazamento de refrigerante R-410A", "kg", 2088.0, 1),
    "van_km": F("Veículo comercial leve (distância)", "km", 0.22, 1),
    "caminhao_leve_km": F("Caminhão leve (distância)", "km", 0.45, 1),
    "caminhao_medio_km": F("Caminhão médio (distância)", "km", 0.75, 1),
    "caminhao_pesado_km": F("Caminhão pesado (distância)", "km", 1.1, 1),
    "cavalo_carreta_km": F("Cavalo mecânico + carreta (distância)", "km", 1.4, 1),
    "frete_ferroviario": F("Frete ferroviário contratado", "t.km", 0.025, 3),
    "frete_aquaviario": F("Frete aquaviário contratado", "t.km", 0.016, 3),
    "frete_aereo": F("Frete aéreo contratado", "t.km", 0.75, 3),
}
def agora(): return dt.datetime.now().isoformat(timespec="seconds")
def erro(msg, code=400): return jsonify(erro=msg), code

def email_permitido(email):
    email = email.strip()
    local, separator, domain = email.rpartition("@")
    return email.count("@") == 1 and bool(local) and separator == "@" and domain.lower() == "carboncheck.com.br"

@contextmanager
def conectar():
    if DB_BACKEND == "mysql":
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(MYSQL_CNF)
        client = parser["client"] if parser.has_section("client") else {}
        settings = {
            "host": os.environ.get("MYSQL_HOST", client.get("host", "localhost")),
            "port": int(os.environ.get("MYSQL_PORT", client.get("port", 3306))),
            "user": os.environ.get("MYSQL_USER", client.get("user", "")),
            "password": os.environ.get("MYSQL_PASSWORD", client.get("password", "")),
            "database": os.environ.get("MYSQL_DATABASE", client.get("database", "carboncheck")),
        }
        if not settings["user"] or not settings["password"]:
            raise RuntimeError(
                "Configure MYSQL_USER/MYSQL_PASSWORD ou o arquivo CARBONCHECK_MYSQL_CNF."
            )
        connection = pymysql.connect(
            **settings,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=10,
        )
        try:
            yield MySQLConnection(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return
    if DB_BACKEND != "sqlite":
        raise RuntimeError("CARBONCHECK_DB_BACKEND deve ser 'sqlite' ou 'mysql'.")

    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

class MySQLConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=()):
        cursor = self.connection.cursor()
        cursor.execute(query.replace("?", "%s"), params)
        return cursor

def inicializar_banco():
    with conectar() as con:
        if DB_BACKEND == "mysql":
            con.execute("""CREATE TABLE IF NOT EXISTS usuarios (
                id VARCHAR(32) PRIMARY KEY, nome VARCHAR(255) NOT NULL,
                email VARCHAR(320) NOT NULL UNIQUE, perfil VARCHAR(40) NOT NULL,
                hash VARCHAR(255) NOT NULL, ativo TINYINT(1) NOT NULL DEFAULT 1
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            con.execute("""CREATE TABLE IF NOT EXISTS inventarios (
                id VARCHAR(32) PRIMARY KEY, org_id VARCHAR(32) NOT NULL,
                periodo SMALLINT NOT NULL, status VARCHAR(40) NOT NULL,
                dados LONGTEXT NOT NULL, UNIQUE KEY uq_inventarios_org_periodo (org_id, periodo),
                INDEX ix_inventarios_org (org_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            colunas = {
                row["Field"] for row in con.execute("SHOW COLUMNS FROM usuarios").fetchall()
            }
            if "ativo" not in colunas:
                con.execute(
                    "ALTER TABLE usuarios ADD COLUMN ativo TINYINT(1) NOT NULL DEFAULT 1"
                )
            return
        con.execute("""CREATE TABLE IF NOT EXISTS usuarios (
            id TEXT PRIMARY KEY, nome TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
            perfil TEXT NOT NULL, hash TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1)""")
        colunas = {
            row["name"] for row in con.execute("PRAGMA table_info(usuarios)")
        }
        if "ativo" not in colunas:
            con.execute(
                "ALTER TABLE usuarios ADD COLUMN ativo INTEGER NOT NULL DEFAULT 1"
            )
        con.execute("""CREATE TABLE IF NOT EXISTS inventarios (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL, periodo INTEGER NOT NULL,
            status TEXT NOT NULL, dados TEXT NOT NULL,
            UNIQUE (org_id, periodo))""")

def usuario_por_id(uid):
    with conectar() as con:
        row = con.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None

def usuario_por_email(email):
    with conectar() as con:
        row = con.execute("SELECT * FROM usuarios WHERE email = ?", (email.lower(),)).fetchone()
    return dict(row) if row else None

def inventario_por_id(iid):
    with conectar() as con:
        row = con.execute("SELECT dados FROM inventarios WHERE id = ?", (iid,)).fetchone()
    return json.loads(row["dados"]) if row else None

def todos_inventarios():
    with conectar() as con:
        rows = con.execute("SELECT dados FROM inventarios").fetchall()
    return [json.loads(row["dados"]) for row in rows]

def salvar_inventario(inv):
    with conectar() as con:
        serialized = json.dumps(inv, ensure_ascii=False)
        if DB_BACKEND == "mysql":
            con.execute(
                """INSERT INTO inventarios (id, org_id, periodo, status, dados)
                   VALUES (?, ?, ?, ?, ?)
                   ON DUPLICATE KEY UPDATE status = VALUES(status), dados = VALUES(dados)""",
                (inv["id"], inv["org_id"], inv["periodo"], inv["status"], serialized),
            )
        else:
            con.execute(
                """INSERT INTO inventarios (id, org_id, periodo, status, dados)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status = excluded.status, dados = excluded.dados""",
                (inv["id"], inv["org_id"], inv["periodo"], inv["status"], serialized),
            )

def novo_usuario(nome, email, senha, perfil):
    email = email.strip().lower()
    if not email_permitido(email):
        raise ValueError("Use um e-mail válido com o domínio @carboncheck.com.br.")
    u = {"id": uuid.uuid4().hex[:8], "nome": nome, "email": email, "perfil": perfil, "hash": generate_password_hash(senha)}
    with conectar() as con:
        con.execute("INSERT INTO usuarios (id, nome, email, perfil, hash) VALUES (?, ?, ?, ?, ?)",
                    (u["id"], u["nome"], u["email"], u["perfil"], u["hash"]))
    return u

def senha_valida(senha):
    return (
        len(senha) >= 8
        and any("A" <= caractere <= "Z" for caractere in senha)
        and any("a" <= caractere <= "z" for caractere in senha)
        and any(not caractere.isalnum() for caractere in senha)
    )

def sessao(u):
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)
    tok = jwt.encode({"sub": u["id"], "exp": exp}, SECRET, algorithm="HS256")
    return jsonify(token=tok, usuario={k: u[k] for k in ("id", "nome", "email", "perfil")})

def requer(*perfis):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            try:
                dados = jwt.decode(request.headers.get("Authorization", "").replace("Bearer ", ""), SECRET, algorithms=["HS256"])
            except jwt.PyJWTError:
                return erro("Token ausente ou inválido.", 401)
            u = usuario_por_id(dados["sub"])
            if not u: return erro("Usuário não encontrado.", 401)
            if not u["ativo"]: return erro("Esta conta está desativada.", 403)
            if perfis and u["perfil"] not in perfis: return erro("Perfil sem permissão para esta ação.", 403)
            g.user = u
            return f(*a, **k)
        return w
    return deco

def pode_ver(u, inv):
    return inv["org_id"] == u["id"] if u["perfil"] == "organizacao" else inv["status"] != "rascunho"

def obter(iid):
    inv = inventario_por_id(iid)
    return inv if inv and pode_ver(g.user, inv) else None

def enquadra(t):
    if t > 25000: return "Acima de 25 mil tCO₂e/ano: inventário anual e metas obrigatórias de redução (Lei 15.042/2024)."
    if t > 10000: return "Acima de 10 mil tCO₂e/ano: inventário anual obrigatório (Lei 15.042/2024)."
    return "Abaixo de 10 mil tCO₂e/ano: fora do enquadramento regulado do SBCE (inventário voluntário)."

def formatar_emissao(tco2e):
    unidade = "kgCO2e" if abs(tco2e) < 1 else "tCO2e"
    valor = tco2e * 1000 if unidade == "kgCO2e" else tco2e
    inteiro, _, decimal = f"{valor:,.2f}".partition(".")
    numero = inteiro.replace(",", ".")
    if decimal.rstrip("0"):
        numero += "," + decimal.rstrip("0")
    return f"{numero} {unidade}"

def publico(inv):
    cat = {}
    for l in inv["lancamentos"]:
        n = CATEGORIAS[l["categoria"]]
        cat[n] = round(cat.get(n, 0) + l["tco2e"], 4)
    total = round(sum(l["tco2e"] for l in inv["lancamentos"]), 4)
    operacoes = inv.get("operacoes", [])
    km_total = sum(op.get("km_total", 0) for op in operacoes)
    km_vazio = sum(op.get("km_vazio", 0) for op in operacoes)
    tkm = sum(op.get("tonelada_km", 0) for op in operacoes)
    emissao_transporte = sum(
        launch["tco2e"] for launch in inv["lancamentos"]
        if launch.get("transporte") or launch.get("categoria") == 3
    )
    return {**inv, "operacoes": operacoes,
            "indicadores_transporte": {
                "km_total": round(km_total, 2), "km_vazio": round(km_vazio, 2),
                "percentual_km_vazio": round(km_vazio * 100 / km_total, 2) if km_total else 0,
                "tonelada_km": round(tkm, 2),
                "emissao_transporte_tco2e": round(emissao_transporte, 4),
                "intensidade_tco2e_tkm": round(emissao_transporte / tkm, 8) if tkm else None,
                "viagens": sum(op.get("viagens", 0) for op in operacoes),
                "veiculos_informados": max((op.get("veiculos", 0) for op in operacoes), default=0),
                "combustivel_l": round(sum(op.get("combustivel_l", 0) for op in operacoes), 2),
                "energia_kwh": round(sum(op.get("energia_kwh", 0) for op in operacoes), 2),
                "refrigerante_kg": round(sum(op.get("refrigerante_kg", 0) for op in operacoes), 3),
                "refrigeracao_h": round(sum(op.get("refrigeracao_h", 0) for op in operacoes), 2),
                "frete_terceirizado_tkm": round(sum(op.get("frete_terceirizado_tkm", 0) for op in operacoes), 2),
            },
            "resumo": {"total_tco2e": total, "por_categoria": cat}, "enquadramento": enquadra(total)}

@app.get("/")
def index(): return send_from_directory("static", "index.html")

@app.get("/healthz")
def healthz():
    try:
        with conectar() as con:
            con.execute("SELECT 1").fetchone()
    except (sqlite3.Error, pymysql.MySQLError):
        app.logger.exception("Falha no banco de dados durante health check.")
        return jsonify(status="indisponivel"), 503
    return jsonify(status="ok"), 200

@app.get("/api/fatores")
def fatores(): return jsonify(FATORES)

@app.post("/api/auth/cadastro")
def cadastro():
    d = request.get_json(silent=True) or {}
    nome = str(d.get("nome", "")).strip()
    email = str(d.get("email", "")).strip()
    senha = str(d.get("senha", ""))
    perfil = str(d.get("perfil", "")).strip()
    if not nome or perfil not in ("organizacao", "verificador", "regulador"):
        return erro("Informe nome e um perfil permitido.")
    if not email_permitido(email):
        return erro("Use um e-mail válido com o domínio @carboncheck.com.br.")
    if not senha_valida(senha):
        return erro("A senha deve ter pelo menos 8 caracteres, uma letra maiúscula, uma letra minúscula e um caractere especial.")
    perfil_conta = perfil if perfil == "organizacao" else f"pendente_{perfil}"
    try:
        u = novo_usuario(nome, email, senha, perfil_conta)
    except (sqlite3.IntegrityError, pymysql.IntegrityError):
        return erro("E-mail já cadastrado.", 409)
    if perfil_conta.startswith("pendente_"):
        return jsonify(
            pendente=True,
            mensagem="Solicitação recebida. O acesso como verificador/regulador será liberado após aprovação administrativa.",
        ), 202
    return sessao(u), 201

@app.post("/api/auth/login")
def login():
    d = request.get_json(silent=True) or {}
    email = str(d.get("email", "")).strip()
    if not email_permitido(email):
        return erro("Use um e-mail válido com o domínio @carboncheck.com.br.")
    u = usuario_por_email(email)
    if not u or not check_password_hash(u["hash"], str(d.get("senha", ""))): return erro("E-mail ou senha incorretos.", 401)
    if not u["ativo"]:
        return erro("Esta conta está desativada. Entre em contato com um administrador.", 403)
    if u["perfil"].startswith("pendente_"):
        return erro("Sua solicitação de perfil aguarda aprovação administrativa.", 403)
    return sessao(u)

@app.post("/api/inventarios")
@requer("organizacao")
def criar():
    try: ano = int((request.get_json(silent=True) or {}).get("periodo"))
    except (TypeError, ValueError): return erro("Informe o período (ano) de apuração.")
    inv = {"id": uuid.uuid4().hex[:8], "org_id": g.user["id"], "organizacao": g.user["nome"], "periodo": ano,
           "status": "rascunho", "lancamentos": [], "operacoes": [], "verificacoes": [], "criado_em": agora()}
    try:
        salvar_inventario(inv)
    except (sqlite3.IntegrityError, pymysql.IntegrityError):
        return erro("Já existe inventário para este período.", 409)
    return jsonify(publico(inv)), 201

@app.get("/api/inventarios")
@requer()
def listar():
    r = [publico(i) for i in todos_inventarios() if pode_ver(g.user, i)]
    return jsonify(sorted(r, key=lambda i: (i["organizacao"], -i["periodo"])))

@app.get("/api/inventarios/<iid>")
@requer()
def detalhe(iid):
    inv = obter(iid)
    return jsonify(publico(inv)) if inv else erro("Inventário não encontrado.", 404)

@app.post("/api/inventarios/<iid>/lancamentos")
@requer("organizacao")
def add_lancamento(iid):
    inv = obter(iid)
    if not inv: return erro("Inventário não encontrado.", 404)
    if inv["status"] not in EDITAVEL: return erro("Inventário não pode ser editado neste status.", 409)
    d = request.get_json(silent=True) or {}
    f = FATORES.get(d.get("fonte"))
    try: q, m = float(d.get("quantidade")), int(d.get("mes"))
    except (TypeError, ValueError): return erro("Quantidade ou mês inválidos.")
    if not f or q <= 0 or not 1 <= m <= 12: return erro("Dados de consumo inválidos.")
    transporte = d.get("transporte") in (True, "true", "on", 1) or f["cat"] == 3
    inv["lancamentos"].append({"id": uuid.uuid4().hex[:8], "fonte": d["fonte"], "nome": f["nome"], "unidade": f["unidade"],
                               "categoria": f["cat"], "transporte": transporte, "mes": m, "quantidade": q,
                               "fator": f["fator"], "tco2e": round(q * f["fator"] / 1000, 6)})
    salvar_inventario(inv)
    return jsonify(publico(inv)), 201

@app.post("/api/inventarios/<iid>/operacoes")
@requer("organizacao")
def add_operacao(iid):
    inv = obter(iid)
    if not inv: return erro("Inventário não encontrado.", 404)
    if inv["status"] not in EDITAVEL: return erro("Inventário não pode ser editado neste status.", 409)
    d = request.get_json(silent=True) or {}
    try:
        mes = int(d.get("mes"))
        km = float(d.get("km_total") or 0)
        km_vazio = float(d.get("km_vazio") or 0)
        carga_t = float(d.get("carga_t") or 0)
        tonelada_km = float(d.get("tonelada_km") or 0)
        viagens = int(d.get("viagens") or 0)
        veiculos = int(d.get("veiculos") or 0)
        combustivel_l = float(d.get("combustivel_l") or 0)
        energia_kwh = float(d.get("energia_kwh") or 0)
        refrigerante_kg = float(d.get("refrigerante_kg") or 0)
        refrigeracao_h = float(d.get("refrigeracao_h") or 0)
        frete_terceirizado_tkm = float(d.get("frete_terceirizado_tkm") or 0)
    except (TypeError, ValueError):
        return erro("Informe valores numéricos válidos para os indicadores da operação.")
    metricas = (km, km_vazio, carga_t, tonelada_km, viagens, veiculos,
                combustivel_l, energia_kwh, refrigerante_kg, refrigeracao_h,
                frete_terceirizado_tkm)
    if (not 1 <= mes <= 12 or not all(math.isfinite(value) for value in metricas)
            or min(metricas) < 0 or km_vazio > km):
        return erro("Confira os indicadores: valores não podem ser negativos, km vazio não pode exceder km total e mês deve estar entre 1 e 12.")
    if km == 0 and km_vazio > 0:
        return erro("Quilometragem vazia exige quilometragem total.")
    modal = str(d.get("modal", "rodoviario")).strip()
    tipo_veiculo = str(d.get("tipo_veiculo", "")).strip()
    if modal not in ("rodoviario", "ferroviario", "aquaviario", "aereo") or not tipo_veiculo:
        return erro("Informe o modal e o tipo de veículo/equipamento.")
    operacao = {
        "id": uuid.uuid4().hex[:8], "mes": mes, "modal": modal,
        "tipo_veiculo": tipo_veiculo[:80], "km_total": km, "km_vazio": km_vazio,
        "carga_t": carga_t, "tonelada_km": tonelada_km or round(carga_t * km, 3),
        "viagens": viagens, "veiculos": veiculos, "combustivel_l": combustivel_l,
        "energia_kwh": energia_kwh, "refrigerante_kg": refrigerante_kg,
        "refrigeracao_h": refrigeracao_h,
        "frete_terceirizado_tkm": frete_terceirizado_tkm,
    }
    inv.setdefault("operacoes", []).append(operacao)
    salvar_inventario(inv)
    return jsonify(publico(inv)), 201

@app.delete("/api/inventarios/<iid>/operacoes/<oid>")
@requer("organizacao")
def del_operacao(iid, oid):
    inv = obter(iid)
    if not inv: return erro("Inventário não encontrado.", 404)
    if inv["status"] not in EDITAVEL: return erro("Inventário não pode ser editado neste status.", 409)
    inv["operacoes"] = [op for op in inv.get("operacoes", []) if op["id"] != oid]
    salvar_inventario(inv)
    return jsonify(publico(inv))

@app.delete("/api/inventarios/<iid>/lancamentos/<lid>")
@requer("organizacao")
def del_lancamento(iid, lid):
    inv = obter(iid)
    if not inv: return erro("Inventário não encontrado.", 404)
    if inv["status"] not in EDITAVEL: return erro("Inventário não pode ser editado neste status.", 409)
    inv["lancamentos"] = [l for l in inv["lancamentos"] if l["id"] != lid]
    salvar_inventario(inv)
    return jsonify(publico(inv))

@app.post("/api/inventarios/<iid>/submeter")
@requer("organizacao")
def submeter(iid):
    inv = obter(iid)
    if not inv: return erro("Inventário não encontrado.", 404)
    if inv["status"] not in EDITAVEL: return erro("Inventário já foi submetido.", 409)
    if not inv["lancamentos"]: return erro("Adicione ao menos um lançamento de emissões antes de submeter.")
    inv["status"], inv["submetido_em"] = "submetido", agora()
    salvar_inventario(inv)
    return jsonify(publico(inv))

@app.post("/api/inventarios/<iid>/verificacao")
@requer("verificador")
def verificar(iid):
    inv = obter(iid)
    if not inv: return erro("Inventário não encontrado.", 404)
    if inv["status"] != "submetido": return erro("Apenas inventários submetidos podem ser verificados.", 409)
    d = request.get_json(silent=True) or {}
    novo = {"aprovar": "aprovado", "ajustar": "ajuste_solicitado", "reprovar": "reprovado"}.get(d.get("decisao"))
    parecer = str(d.get("parecer", "")).strip()
    if not novo or len(parecer) < 5: return erro("Informe a decisão e o parecer técnico.")
    inv["verificacoes"].append({"decisao": novo, "parecer": parecer, "verificador": g.user["nome"],
                                "verificador_id": g.user["id"], "data": agora()})
    inv["status"] = novo
    salvar_inventario(inv)
    return jsonify(publico(inv))

def gerar_pdf(d):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4); y = [800]
    def ln(t, s=10, b=False):
        for p in textwrap.wrap(t.replace("₂", "2").replace("³", "3"), 95) or [""]:
            if y[0] < 60: c.showPage(); y[0] = 800
            c.setFont("Helvetica-Bold" if b else "Helvetica", s); c.drawString(50, y[0], p); y[0] -= s + 5
    ln("CarbonCheck - Relatório do Inventário de Emissões de GEE", 14, True)
    ln(f"Organização: {d['organizacao']} | Período: {d['periodo']} | Status: {STATUS[d['status']]}")
    ln(f"Total: {formatar_emissao(d['resumo']['total_tco2e'])} (ISO 14064-1, fatores simplificados)", 12, True)
    ln(d["enquadramento"]); ln("")
    ln("Emissões por categoria", 11, True)
    for k, v in d["resumo"]["por_categoria"].items(): ln(f"  {k}: {formatar_emissao(v)}")
    indicadores = d.get("indicadores_transporte", {})
    ln(""); ln("Indicadores operacionais de transporte", 11, True)
    ln(f"  Distância: {indicadores.get('km_total', 0)} km | Distância vazia: {indicadores.get('km_vazio', 0)} km ({indicadores.get('percentual_km_vazio', 0)}%)")
    ln(f"  Produtividade: {indicadores.get('tonelada_km', 0)} t.km | Viagens: {indicadores.get('viagens', 0)} | Frota informada: {indicadores.get('veiculos_informados', 0)} veículos")
    ln(f"  Consumo reportado: {indicadores.get('combustivel_l', 0)} L | Energia de tração: {indicadores.get('energia_kwh', 0)} kWh")
    ln(f"  Refrigerante: {indicadores.get('refrigerante_kg', 0)} kg | Refrigeração: {indicadores.get('refrigeracao_h', 0)} h | Frete terceirizado: {indicadores.get('frete_terceirizado_tkm', 0)} t.km")
    for operacao in d.get("operacoes", []):
        ln(f"  Mês {operacao['mes']:02d} | {operacao['modal']} | {operacao['tipo_veiculo']} | {operacao['km_total']} km | {operacao['tonelada_km']} t.km")
    ln(""); ln("Lançamentos", 11, True)
    for l in d["lancamentos"]: ln(f"  Mês {l['mes']:02d} | {l['nome']} | {l['quantidade']} {l['unidade']} x {l['fator']} kgCO2e = {formatar_emissao(l['tco2e'])}")
    ln(""); ln("Verificação independente (ISO 14064-3)", 11, True)
    if not d["verificacoes"]: ln("  Sem parecer de verificador até o momento.")
    for v in d["verificacoes"]:
        nome_verificador = v.get("verificador", "Identidade removida")
        ln(f"  {v['data']} | {nome_verificador} | {STATUS[v['decisao']]}: {v['parecer']}")
    c.save(); buf.seek(0)
    return buf

@app.get("/api/inventarios/<iid>/relatorio")
@requer()
def relatorio(iid):
    inv = obter(iid)
    if not inv: return erro("Inventário não encontrado.", 404)
    d = publico(inv)
    if request.args.get("formato") == "pdf":
        return send_file(gerar_pdf(d), mimetype="application/pdf", as_attachment=True, download_name=f"inventario-{iid}.pdf")
    return jsonify({"norma": "ISO 14064-1 / ISO 14064-3 (simplificado)", "gerado_em": agora(), **d})

@app.get("/api/historico")
@requer()
def historico():
    orgs = {}
    for inv in sorted(todos_inventarios(), key=lambda i: i["periodo"]):
        if not pode_ver(g.user, inv): continue
        o = orgs.setdefault(inv["org_id"], {"organizacao": inv["organizacao"], "inventarios": []})
        o["inventarios"].append({"id": inv["id"], "periodo": inv["periodo"], "status": inv["status"], "total_tco2e": publico(inv)["resumo"]["total_tco2e"]})
    for o in orgs.values():
        v = [i for i in o["inventarios"] if i["status"] != "rascunho"]
        o["conformidade"] = "Sem inventário" if not v else {"aprovado": "Conforme", "reprovado": "Não conforme"}.get(v[-1]["status"], "Em análise")
    return jsonify(list(orgs.values()))

@app.get("/api/painel/transporte")
@requer("verificador", "regulador", "administrador")
def painel_transporte():
    inventarios_enviados = [
        inv for inv in todos_inventarios()
        if inv["status"] != "rascunho"
    ]
    inventarios_aprovados = [
        inv for inv in inventarios_enviados if inv["status"] == "aprovado"
    ]
    anos_disponiveis = sorted({inv["periodo"] for inv in inventarios_aprovados})
    ano_parametro = request.args.get("ano")
    try:
        ano_selecionado = int(ano_parametro) if ano_parametro else None
    except ValueError:
        return erro("Ano inválido.")

    tendencia_anual = {}
    for inv in inventarios_aprovados:
        dados = publico(inv)
        year = tendencia_anual.setdefault(str(inv["periodo"]), {
            "periodo": inv["periodo"], "emissoes_tco2e": 0.0,
        })
        year["emissoes_tco2e"] += dados["resumo"]["total_tco2e"]

    inventarios_status = [
        inv for inv in inventarios_enviados
        if ano_selecionado is None or inv["periodo"] == ano_selecionado
    ]
    inventarios = [
        inv for inv in inventarios_aprovados
        if ano_selecionado is None or inv["periodo"] == ano_selecionado
    ]
    totals = {
        "inventarios": len(inventarios),
        "transportadoras": len({inv["org_id"] for inv in inventarios}),
        "emissoes_tco2e": 0.0, "emissao_transporte_tco2e": 0.0,
        "km_total": 0.0, "km_vazio": 0.0, "tonelada_km": 0.0,
        "viagens": 0, "combustivel_l": 0.0, "energia_kwh": 0.0,
        "refrigerante_kg": 0.0, "refrigeracao_h": 0.0,
        "frete_terceirizado_tkm": 0.0,
    }
    status_counts = {status: 0 for status in STATUS}
    for inv in inventarios_status:
        status_counts[inv["status"]] = status_counts.get(inv["status"], 0) + 1
    por_categoria, por_modal, por_org = {}, {}, {}
    frota_por_org = {}
    for inv in inventarios:
        dados = publico(inv)
        indic = dados["indicadores_transporte"]
        emissions = dados["resumo"]["total_tco2e"]
        totals["emissoes_tco2e"] += emissions
        totals["emissao_transporte_tco2e"] += sum(
            launch["tco2e"] for launch in inv.get("lancamentos", [])
            if launch.get("transporte") or launch.get("categoria") == 3
        )
        for key in ("km_total", "km_vazio", "tonelada_km", "viagens", "combustivel_l",
                    "energia_kwh", "refrigerante_kg", "refrigeracao_h",
                    "frete_terceirizado_tkm"):
            totals[key] += indic[key]
        for categoria, valor in dados["resumo"]["por_categoria"].items():
            por_categoria[categoria] = por_categoria.get(categoria, 0.0) + valor
        org = por_org.setdefault(inv["org_id"], {
            "organizacao": inv["organizacao"], "inventarios": 0,
            "emissoes_tco2e": 0.0, "emissao_transporte_tco2e": 0.0,
            "km_total": 0.0, "tonelada_km": 0.0,
            "status": inv["status"], "_periodo_mais_recente": -1,
        })
        org["inventarios"] += 1
        org["emissoes_tco2e"] += emissions
        org["emissao_transporte_tco2e"] += dados["indicadores_transporte"]["emissao_transporte_tco2e"]
        org["km_total"] += indic["km_total"]
        org["tonelada_km"] += indic["tonelada_km"]
        if inv["periodo"] >= org["_periodo_mais_recente"]:
            org["_periodo_mais_recente"] = inv["periodo"]
            org["status"] = inv["status"]
            frota_por_org[inv["org_id"]] = indic["veiculos_informados"]
        for op in inv.get("operacoes", []):
            modal = op.get("modal", "rodoviario")
            bucket = por_modal.setdefault(modal, {"operacoes": 0, "km_total": 0.0, "tonelada_km": 0.0, "viagens": 0})
            bucket["operacoes"] += 1
            bucket["km_total"] += op.get("km_total", 0)
            bucket["tonelada_km"] += op.get("tonelada_km", 0)
            bucket["viagens"] += op.get("viagens", 0)
    totals["frota_informada"] = sum(frota_por_org.values())
    totals["percentual_km_vazio"] = round(totals["km_vazio"] * 100 / totals["km_total"], 2) if totals["km_total"] else 0
    totals["intensidade_tco2e_tkm"] = round(totals["emissao_transporte_tco2e"] / totals["tonelada_km"], 8) if totals["tonelada_km"] else None
    rounded_totals = {
        key: round(value, 8 if key == "intensidade_tco2e_tkm" else 4)
        if isinstance(value, float) else value
        for key, value in totals.items()
    }
    for item in tendencia_anual.values():
        item["emissoes_tco2e"] = round(item["emissoes_tco2e"], 4)
    for item in por_org.values():
        for key, value in item.items():
            if isinstance(value, float):
                item[key] = round(value, 4)
    for item in por_org.values():
        item.pop("_periodo_mais_recente", None)
        item["intensidade_tco2e_tkm"] = round(item["emissao_transporte_tco2e"] / item["tonelada_km"], 8) if item["tonelada_km"] else None
    for item in por_modal.values():
        for key, value in item.items():
            if isinstance(value, float):
                item[key] = round(value, 2)
    return jsonify({
        "ano_selecionado": ano_selecionado,
        "anos_disponiveis": anos_disponiveis,
        "totais": rounded_totals, "por_status": status_counts,
        "por_categoria": sorted(
            ({"categoria": categoria, "emissoes_tco2e": round(valor, 4)}
             for categoria, valor in por_categoria.items()),
            key=lambda item: item["emissoes_tco2e"], reverse=True,
        ),
        "por_ano": sorted(tendencia_anual.values(), key=lambda item: item["periodo"]),
        "por_modal": por_modal,
        "por_transportadora": sorted(por_org.values(), key=lambda item: item["emissoes_tco2e"], reverse=True),
    })

@app.get("/api/admin/solicitacoes")
@requer("administrador")
def listar_solicitacoes():
    with conectar() as con:
        rows = con.execute(
            """SELECT id, nome, email, perfil FROM usuarios
               WHERE perfil IN ('pendente_verificador', 'pendente_regulador')
               ORDER BY nome, email"""
        ).fetchall()
    return jsonify([
        {**dict(row), "perfil": row["perfil"].removeprefix("pendente_")}
        for row in rows
    ])

@app.get("/api/admin/usuarios")
@requer("administrador")
def listar_usuarios_admin():
    with conectar() as con:
        rows = con.execute(
            "SELECT id, nome, email, perfil, ativo FROM usuarios ORDER BY nome, email"
        ).fetchall()
    usuarios = []
    for row in rows:
        perfil = row["perfil"]
        status = (
            "desativado" if not row["ativo"]
            else "pendente_aprovacao" if perfil.startswith("pendente_")
            else "ativo"
        )
        usuarios.append({
            "id": row["id"],
            "nome": row["nome"],
            "email": row["email"],
            "perfil": perfil.removeprefix("pendente_"),
            "status": status,
        })
    return jsonify(usuarios)

@app.post("/api/admin/usuarios/<uid>/status")
@requer("administrador")
def alterar_status_usuario(uid):
    d = request.get_json(silent=True) or {}
    ativo = d.get("ativo")
    if not isinstance(ativo, bool):
        return erro("Informe ativo como verdadeiro ou falso.")
    if uid == g.user["id"] and not ativo:
        return erro("Você não pode desativar sua própria conta.", 409)

    with conectar() as con:
        row = con.execute(
            "SELECT perfil, ativo FROM usuarios WHERE id = ?", (uid,)
        ).fetchone()
        if not row:
            return erro("Conta não encontrada.", 404)
        if not ativo and row["perfil"] == "administrador":
            outros_admins = con.execute(
                """SELECT COUNT(*) FROM usuarios
                   WHERE perfil = 'administrador' AND ativo = 1 AND id != ?""",
                (uid,),
            ).fetchone()[0]
            if outros_admins == 0:
                return erro("Não é possível desativar o último administrador ativo.", 409)
        con.execute("UPDATE usuarios SET ativo = ? WHERE id = ?", (int(ativo), uid))

    status = "desativado" if not ativo else (
        "pendente_aprovacao" if row["perfil"].startswith("pendente_") else "ativo"
    )
    return jsonify(status=status)

@app.delete("/api/admin/usuarios/<uid>")
@requer("administrador")
def excluir_usuario_admin(uid):
    if uid == g.user["id"]:
        return erro("Você não pode excluir sua própria conta.", 409)

    with conectar() as con:
        usuario = con.execute(
            "SELECT nome, perfil FROM usuarios WHERE id = ?", (uid,)
        ).fetchone()
        if not usuario:
            return erro("Conta não encontrada.", 404)
        if usuario["perfil"] == "administrador":
            outros_admins = con.execute(
                """SELECT COUNT(*) FROM usuarios
                   WHERE perfil = 'administrador' AND ativo = 1 AND id != ?""",
                (uid,),
            ).fetchone()[0]
            if outros_admins == 0:
                return erro("Não é possível excluir o último administrador ativo.", 409)

        removidos = con.execute(
            "DELETE FROM inventarios WHERE org_id = ?", (uid,)
        ).rowcount
        inventarios = con.execute(
            "SELECT id, dados FROM inventarios"
        ).fetchall()
        pareceres_anomizados = 0
        for inventario in inventarios:
            dados = json.loads(inventario["dados"])
            alterado = False
            for parecer in dados.get("verificacoes", []):
                id_corresponde = parecer.get("verificador_id") == uid
                legado_corresponde = (
                    "verificador_id" not in parecer
                    and parecer.get("verificador") == usuario["nome"]
                )
                if id_corresponde or legado_corresponde:
                    parecer.pop("verificador", None)
                    parecer.pop("verificador_id", None)
                    alterado = True
            if alterado:
                pareceres_anomizados += 1
                con.execute(
                    "UPDATE inventarios SET dados = ? WHERE id = ?",
                    (json.dumps(dados, ensure_ascii=False), inventario["id"]),
                )
        con.execute("DELETE FROM usuarios WHERE id = ?", (uid,))

    return jsonify(
        removida=True,
        inventarios_removidos=removidos,
        inventarios_com_pareceres_anomizados=pareceres_anomizados,
    )

@app.post("/api/admin/solicitacoes/<uid>/aprovar")
@requer("administrador")
def aprovar_solicitacao(uid):
    with conectar() as con:
        row = con.execute(
            "SELECT perfil FROM usuarios WHERE id = ?", (uid,)
        ).fetchone()
        if not row or row["perfil"] not in (
            "pendente_verificador", "pendente_regulador"
        ):
            return erro("Solicitação pendente não encontrada.", 404)
        perfil = row["perfil"].removeprefix("pendente_")
        con.execute("UPDATE usuarios SET perfil = ? WHERE id = ?", (perfil, uid))
    return jsonify(aprovado=True, perfil=perfil)

@app.delete("/api/admin/solicitacoes/<uid>")
@requer("administrador")
def rejeitar_solicitacao(uid):
    with conectar() as con:
        result = con.execute(
            """DELETE FROM usuarios
               WHERE id = ? AND perfil IN ('pendente_verificador', 'pendente_regulador')""",
            (uid,),
        )
    if result.rowcount == 0:
        return erro("Solicitação pendente não encontrada.", 404)
    return jsonify(removida=True)

inicializar_banco()
if APP_ENV != "production":
    for n, e, old_e, p in (
        ("Empresa Demo Ltda", "org@carboncheck.com.br", "org@demo.com", "organizacao"),
        ("Verificadora Demo", "verificador@carboncheck.com.br", "verificador@demo.com", "verificador"),
        ("Órgão Regulador Demo", "regulador@carboncheck.com.br", "regulador@demo.com", "regulador"),
        ("Administrador Demo", "admin@carboncheck.com.br", "admin@demo.com", "administrador"),
    ):
        if not usuario_por_email(e):
            with conectar() as con:
                con.execute("UPDATE usuarios SET email = ? WHERE email = ?", (e, old_e))
            if usuario_por_email(e):
                with conectar() as con:
                    con.execute(
                        "UPDATE usuarios SET hash = ? WHERE email = ?",
                        (generate_password_hash("demo1234"), e),
                    )
                continue
            novo_usuario(n, e, "demo1234", p)
        else:
            with conectar() as con:
                con.execute(
                    "UPDATE usuarios SET hash = ? WHERE email = ?",
                    (generate_password_hash("demo1234"), e),
                )

@app.cli.command("create-user")
def create_user():
    """Cria uma conta privilegiada para verificação/regulação."""
    nome = prompt("Nome")
    email = prompt("E-mail").strip().lower()
    if not email_permitido(email):
        raise ClickException("Use um e-mail com o domínio @carboncheck.com.br.")
    perfil = prompt("Perfil", type=Choice(["verificador", "regulador"]))
    senha = prompt("Senha (mínimo 8 caracteres, maiúscula, minúscula e caractere especial)", hide_input=True, confirmation_prompt=True)
    if not senha_valida(senha):
        raise ClickException("A senha deve ter pelo menos 8 caracteres, uma letra maiúscula, uma letra minúscula e um caractere especial.")
    try:
        novo_usuario(nome, email, senha, perfil)
    except (sqlite3.IntegrityError, pymysql.IntegrityError) as exc:
        raise ClickException("E-mail já cadastrado.") from exc
    print(f"Conta {perfil} criada para {email}.")

@app.cli.command("create-admin")
def create_admin():
    """Cria uma conta administradora com credenciais definidas pelo operador."""
    nome = prompt("Nome")
    email = prompt("E-mail").strip().lower()
    if not email_permitido(email):
        raise ClickException("Use um e-mail com o domínio @carboncheck.com.br.")
    senha = prompt("Senha (mínimo 8 caracteres, maiúscula, minúscula e caractere especial)", hide_input=True, confirmation_prompt=True)
    if not senha_valida(senha):
        raise ClickException("A senha deve ter pelo menos 8 caracteres, uma letra maiúscula, uma letra minúscula e um caractere especial.")
    try:
        novo_usuario(nome, email, senha, "administrador")
    except (sqlite3.IntegrityError, pymysql.IntegrityError) as exc:
        raise ClickException("E-mail já cadastrado.") from exc
    print(f"Conta administradora criada para {email}.")

@app.cli.command("approve-user")
def approve_user():
    """Aprova uma solicitação de conta de verificador ou regulador."""
    email = prompt("E-mail da conta a aprovar").strip().lower()
    u = usuario_por_email(email)
    if not u or u["perfil"] not in ("pendente_verificador", "pendente_regulador"):
        raise ClickException("Não há solicitação pendente para esse e-mail.")
    perfil = u["perfil"].removeprefix("pendente_")
    with conectar() as con:
        con.execute("UPDATE usuarios SET perfil = ? WHERE id = ?", (perfil, u["id"]))
    print(f"Conta {perfil} aprovada para {email}.")

@app.cli.command("pending-users")
def pending_users():
    """Lista solicitações pendentes de verificador/regulador."""
    with conectar() as con:
        rows = con.execute(
            """SELECT nome, email, perfil FROM usuarios
               WHERE perfil IN ('pendente_verificador', 'pendente_regulador')
               ORDER BY nome, email"""
        ).fetchall()
    if not rows:
        print("Não há solicitações pendentes.")
        return
    for row in rows:
        print(f"{row['nome']} | {row['email']} | {row['perfil'].removeprefix('pendente_')}")

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=False)
