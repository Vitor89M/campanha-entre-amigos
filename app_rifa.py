import os
import uuid
import base64
from datetime import datetime, timedelta, timezone
import requests
import streamlit as st
from supabase import create_client

# ==============================================================================
# CONFIGURAÇÃO DA PÁGINA (VISUAL LIMPO PARA CELULAR)
# ==============================================================================
st.set_page_config(page_title="Campanha Rápida", page_icon="🎟️", layout="centered")

st.markdown(
    """
    <style>
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        header {visibility: hidden;}
        .block-container {
            padding-top: 1rem;
            padding-bottom: 4rem;
            max-width: 520px;
        }
        div.stButton > button {
            width: 100%;
            border-radius: 10px;
            font-weight: 700;
            min-height: 2.6rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# ==============================================================================
# VARIÁVEIS DE AMBIENTE / SECRETS
# ==============================================================================
def get_secret(name: str) -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    return os.getenv(name, "").strip()


SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_SERVICE_ROLE_KEY")
MP_ACCESS_TOKEN = get_secret("MP_ACCESS_TOKEN")

RIFA_CONFIG = {
    "titulo": "🏆 Campanha Oficial - Prêmio de R$ 750,00 no Pix",
    "descricao": (
        "Escolha seus números favoritos, aproveite os combos com desconto, "
        "preencha seus dados e gere o Pix na hora. Assim que o pagamento é "
        "confirmado, seus números ficam garantidos."
    ),
    "regras": (
        "1. 1 número = R$ 15,00 | Combo 2 números = R$ 28,00 | Combo 4 números = R$ 55,00.\n"
        "2. Os combos são aplicados automaticamente na soma do carrinho.\n"
        "3. A reserva via Pix tem validade de 15 minutos; após esse prazo, números não pagos voltam a ficar livres.\n"
        "4. Informe um telefone com WhatsApp e e-mail válidos para conferência.\n"
        "5. O sorteio será realizado conforme a data/critério divulgado pelo organizador."
    ),
    "valor_cota": 15.00,
    "valor_combo_2": 28.00,
    "valor_combo_4": 55.00,
    "valor_premio": 750.00,
    "meta_liquida": 2000.00,
    "total_numeros": 200,
    "colunas": 5,
    "minutos_reserva": 15,
}


@st.cache_resource
def get_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_KEY)


sb = get_supabase()


# ==============================================================================
# FUNÇÕES DE CÁLCULO, BANCO (SUPABASE) E MERCADO PAGO
# ==============================================================================
def calcular_valor_combos(qtd: int) -> dict:
    """Calcula o total priorizando pacotes de 4 (R$ 55), 2 (R$ 28) e avulsos (R$ 15)."""
    if qtd <= 0:
        return {
            "qtd": 0,
            "combos_4": 0,
            "combos_2": 0,
            "avulsos": 0,
            "valor_total": 0.0,
            "economia": 0.0,
            "resumo_partes": [],
            "codigo_combo": "nenhum",
        }

    combos_4, resto_4 = divmod(qtd, 4)
    combos_2, avulsos = divmod(resto_4, 2)

    valor_total = round(
        combos_4 * RIFA_CONFIG["valor_combo_4"]
        + combos_2 * RIFA_CONFIG["valor_combo_2"]
        + avulsos * RIFA_CONFIG["valor_cota"],
        2,
    )
    valor_sem_desconto = round(qtd * RIFA_CONFIG["valor_cota"], 2)
    economia = round(max(0.0, valor_sem_desconto - valor_total), 2)

    resumo_partes = []
    if combos_4:
        resumo_partes.append(f"{combos_4}x combo de 4 (R$ {combos_4 * RIFA_CONFIG['valor_combo_4']:.2f})")
    if combos_2:
        resumo_partes.append(f"{combos_2}x combo de 2 (R$ {combos_2 * RIFA_CONFIG['valor_combo_2']:.2f})")
    if avulsos:
        resumo_partes.append(f"{avulsos}x avulso (R$ {avulsos * RIFA_CONFIG['valor_cota']:.2f})")

    tipos_usados = sum([bool(combos_4), bool(combos_2), bool(avulsos)])
    if tipos_usados > 1:
        codigo_combo = "misto"
    elif combos_4:
        codigo_combo = "combo_4"
    elif combos_2:
        codigo_combo = "combo_2"
    else:
        codigo_combo = "avulso"

    return {
        "qtd": qtd,
        "combos_4": combos_4,
        "combos_2": combos_2,
        "avulsos": avulsos,
        "valor_total": valor_total,
        "economia": economia,
        "resumo_partes": resumo_partes,
        "codigo_combo": codigo_combo,
    }


def limpar_reservas_expiradas():
    if not sb:
        return
    agora_iso = datetime.now(timezone.utc).isoformat()
    sb.table("reservas_rifa").delete().eq("status", "pendente").lt("expira_em", agora_iso).execute()


def buscar_reservas() -> list[dict]:
    if not sb:
        return []
    res = sb.table("reservas_rifa").select("numero, status, valor_total, payment_id").execute()
    return res.data or []


def resumir_campanha(reservas: list[dict]) -> dict:
    """Agrupa por payment_id (ou número) para evitar somar valor_total duplicado em compras múltiplas."""
    ocupados = {}
    grupos_pagos = {}
    grupos_pendentes = {}

    for row in reservas:
        num = int(row["numero"])
        status = str(row.get("status", "")).lower()
        ocupados[num] = status
        chave = str(row.get("payment_id") or f"num_{num}")
        valor = float(row.get("valor_total") or 0.0)

        if status == "pago":
            grupos_pagos[chave] = valor
        elif status == "pendente":
            grupos_pendentes[chave] = valor

    arrecadado_pago = round(sum(grupos_pagos.values()), 2)
    pendente_pix = round(sum(grupos_pendentes.values()), 2)
    lucro_liquido_atual = round(arrecadado_pago - RIFA_CONFIG["valor_premio"], 2)
    qtd_pagos = sum(1 for st_item in ocupados.values() if st_item == "pago")
    qtd_pendentes = sum(1 for st_item in ocupados.values() if st_item == "pendente")
    qtd_livres = RIFA_CONFIG["total_numeros"] - qtd_pagos - qtd_pendentes

    return {
        "ocupados": ocupados,
        "arrecadado_pago": arrecadado_pago,
        "pendente_pix": pendente_pix,
        "lucro_liquido_atual": lucro_liquido_atual,
        "qtd_pagos": qtd_pagos,
        "qtd_pendentes": qtd_pendentes,
        "qtd_livres": qtd_livres,
    }


def criar_pix_mercado_pago(numeros: list[int], nome: str, telefone: str, email: str, valor_total: float) -> dict:
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

    partes_nome = nome.strip().split()
    first_name = partes_nome[0]
    last_name = " ".join(partes_nome[1:]) if len(partes_nome) > 1 else "Participante"

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": str(uuid.uuid4()),
    }
    payload = {
        "transaction_amount": round(float(valor_total), 2),
        "description": f"Rifa - Números: {', '.join(f'{n:03d}' for n in sorted(numeros))}",
        "payment_method_id": "pix",
        "payer": {
            "email": email.strip(),
            "first_name": first_name,
            "last_name": last_name,
        },
    }

    resp = requests.post("https://api.mercadopago.com/v1/payments", json=payload, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    qr_code = tx.get("qr_code")
    qr_base64 = tx.get("qr_code_base64")

    if not qr_code or not qr_base64:
        raise RuntimeError("Mercado Pago não retornou QR Code Pix.")

    return {
        "payment_id": str(data["id"]),
        "qr_code": qr_code,
        "qr_code_base64": qr_base64,
    }


def consultar_status_pix(payment_id: str) -> str:
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    resp = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}", headers=headers, timeout=15)
    resp.raise_for_status()
    return str(resp.json().get("status", "pending")).lower()


def marcar_pagamento_como_pago(payment_id: str):
    if not sb:
        return
    sb.table("reservas_rifa").update({"status": "pago"}).eq("payment_id", str(payment_id)).execute()


def reservar_numeros_e_gerar_pix(numeros: list[int], nome: str, telefone: str, email: str) -> dict:
    if not sb:
        raise RuntimeError("Configure SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY antes de reservar.")

    limpar_reservas_expiradas()
    numeros_ordenados = sorted(set(numeros))
    calculo = calcular_valor_combos(len(numeros_ordenados))
    valor_total = calculo["valor_total"]
    expira_em = (datetime.now(timezone.utc) + timedelta(minutes=RIFA_CONFIG["minutos_reserva"])).isoformat()

    # 1. Reserva os números primeiro no Supabase para evitar conflito entre compradores
    linhas = [
        {
            "numero": n,
            "nome": nome.strip(),
            "telefone": telefone.strip(),
            "email": email.strip(),
            "status": "pendente",
            "payment_id": None,
            "valor_total": valor_total,
            "expira_em": expira_em,
        }
        for n in numeros_ordenados
    ]
    sb.table("reservas_rifa").insert(linhas).execute()

    # 2. Gera o Pix no Mercado Pago; se falhar, libera os números reservados
    try:
        pix = criar_pix_mercado_pago(numeros_ordenados, nome, telefone, email, valor_total)
    except Exception:
        sb.table("reservas_rifa").delete().in_("numero", numeros_ordenados).eq("status", "pendente").execute()
        raise

    # 3. Vincula o payment_id às linhas reservadas
    sb.table("reservas_rifa").update({"payment_id": pix["payment_id"]}).in_("numero", numeros_ordenados).execute()

    return {
        "payment_id": pix["payment_id"],
        "numeros": numeros_ordenados,
        "valor_total": valor_total,
        "economia": calculo["economia"],
        "detalhe_combos": " + ".join(calculo["resumo_partes"]),
        "qr_code": pix["qr_code"],
        "qr_code_base64": pix["qr_code_base64"],
        "status_pagamento": "pending",
    }


# ==============================================================================
# ESTADO DA SESSÃO
# ==============================================================================
if "selecionados" not in st.session_state:
    st.session_state.selecionados = []
if "pix_atual" not in st.session_state:
    st.session_state.pix_atual = None


# ==============================================================================
# CABEÇALHO E REGRAS
# ==============================================================================
st.title(RIFA_CONFIG["titulo"])
st.write(RIFA_CONFIG["descricao"])

with st.expander("📜 Regras e Combos da Campanha", expanded=True):
    st.markdown(RIFA_CONFIG["regras"].replace("\n", "  \n"))

# ==============================================================================
# VERIFICAÇÃO DE CREDENCIAIS E CONEXÃO COM SUPABASE
# ==============================================================================
if not SUPABASE_URL or not SUPABASE_KEY or not MP_ACCESS_TOKEN:
    st.warning(
        "Configuração pendente: defina `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` "
        "e `MP_ACCESS_TOKEN` nos Secrets do Streamlit Cloud (ou variáveis de ambiente)."
    )

conexao_ok = False
resumo_campanha = {
    "ocupados": {},
    "arrecadado_pago": 0.0,
    "pendente_pix": 0.0,
    "lucro_liquido_atual": -RIFA_CONFIG["valor_premio"],
    "qtd_pagos": 0,
    "qtd_pendentes": 0,
    "qtd_livres": RIFA_CONFIG["total_numeros"],
}

if sb:
    try:
        limpar_reservas_expiradas()
        reservas_atuais = buscar_reservas()
        resumo_campanha = resumir_campanha(reservas_atuais)
        conexao_ok = True
    except Exception as exc:
        st.error(f"Erro ao conectar no Supabase: {exc}")
        st.info(
            "Confira se a `SUPABASE_URL` está no formato `https://<ref>.supabase.co`, "
            "sem espaços extras, e se o projeto no Supabase está ativo."
        )
        if st.button("🔄 Tentar reconectar"):
            st.cache_resource.clear()
            st.rerun()

if not conexao_ok:
    st.warning("A seleção de números fica temporariamente indisponível enquanto a conexão com o banco não é restabelecida.")
    st.stop()

ocupados = resumo_campanha["ocupados"]

# Remove da seleção local algum número que acabou de ser ocupado por outra pessoa
st.session_state.selecionados = [n for n in st.session_state.selecionados if n not in ocupados]


# ==============================================================================
# RESUMO DA CAMPANHA, DADOS, GRADE DE NÚMEROS E PIX
# ==============================================================================
with st.expander("📊 Resumo da Campanha (Prêmio R$ 750 | Meta R$ 2.000)", expanded=False):
    c1, c2, c3 = st.columns(3)
    c1.metric("Pagos", f"{resumo_campanha['qtd_pagos']}/{RIFA_CONFIG['total_numeros']}")
    c2.metric("Arrecadado", f"R$ {resumo_campanha['arrecadado_pago']:.2f}")
    c3.metric("Líquido vs Prêmio", f"R$ {resumo_campanha['lucro_liquido_atual']:.2f}")
    st.caption(
        f"Livres: **{resumo_campanha['qtd_livres']}** · "
        f"Reservados (aguardando Pix): **{resumo_campanha['qtd_pendentes']}** · "
        f"Meta líquida: **R$ {RIFA_CONFIG['meta_liquida']:.2f}**"
    )

st.caption(
    "1 número: **R$ 15,00** · 2 números: **R$ 28,00** · 4 números: **R$ 55,00** · "
    "🟢 Livre | 🟡 Reservado | 🔴 Pago"
)

st.divider()

# 1. Dados do comprador
st.subheader("1. Seus dados")
nome = st.text_input("Nome completo", placeholder="Ex: Carlos Eduardo")
telefone = st.text_input("Telefone / WhatsApp", placeholder="Ex: 11999998888")
email = st.text_input("E-mail", placeholder="Ex: carlos@email.com")

st.divider()

# 2. Grade de números (001 a 200)
st.subheader("2. Escolha seu(s) número(s)")

cols = st.columns(RIFA_CONFIG["colunas"])
for num in range(1, RIFA_CONFIG["total_numeros"] + 1):
    col = cols[(num - 1) % RIFA_CONFIG["colunas"]]
    status = ocupados.get(num)

    if status == "pago":
        col.button(f"🔴 {num:03d}", key=f"num_{num}", disabled=True)
    elif status == "pendente":
        col.button(f"🟡 {num:03d}", key=f"num_{num}", disabled=True)
    else:
        esta_marcado = num in st.session_state.selecionados
        rotulo = f"🟢 {num:03d} ✓" if esta_marcado else f"🟢 {num:03d}"
        if col.button(rotulo, key=f"num_{num}"):
            if esta_marcado:
                st.session_state.selecionados.remove(num)
            else:
                st.session_state.selecionados.append(num)
            st.rerun()

# 3. Soma com combos e geração do Pix
qtd_selecionados = len(st.session_state.selecionados)
calculo_atual = calcular_valor_combos(qtd_selecionados)
soma_total = calculo_atual["valor_total"]

st.divider()
st.subheader("3. Resumo e Pagamento Pix")

if qtd_selecionados > 0:
    numeros_fmt = ", ".join(f"{n:03d}" for n in sorted(st.session_state.selecionados))
    detalhe_combos = " + ".join(calculo_atual["resumo_partes"])
    linha_economia = (
        f"  \n🎉 **Economia no combo:** R$ {calculo_atual['economia']:.2f}"
        if calculo_atual["economia"] > 0
        else ""
    )
    st.info(
        f"**Números escolhidos ({qtd_selecionados}):** {numeros_fmt}  \n"
        f"**Composição:** {detalhe_combos}  \n"
        f"**Valor total:** **R$ {soma_total:.2f}**{linha_economia}"
    )
else:
    st.caption("Selecione um ou mais números verdes acima (1 por R$ 15, 2 por R$ 28 ou 4 por R$ 55).")

if st.button("💠 Gerar QR Code Pix Mercado Pago", type="primary", disabled=(qtd_selecionados == 0)):
    if not nome.strip() or not telefone.strip() or not email.strip():
        st.error("Preencha nome, telefone e e-mail antes de gerar o Pix.")
    elif "@" not in email or "." not in email:
        st.error("Digite um e-mail válido.")
    else:
        try:
            with st.spinner("Reservando números e gerando Pix no Mercado Pago..."):
                resultado = reservar_numeros_e_gerar_pix(
                    st.session_state.selecionados, nome, telefone, email
                )
            st.session_state.pix_atual = resultado
            st.session_state.selecionados = []
            st.rerun()
        except Exception as exc:
            msg = str(exc)
            if "duplicate key" in msg.lower() or "unique" in msg.lower():
                st.error("Um ou mais números escolhidos acabaram de ser reservados por outra pessoa. Atualize a grade.")
                st.rerun()
            else:
                st.error(f"Não foi possível gerar o Pix agora: {exc}")

# 4. Exibição do QR Code Pix e conferência de pagamento
if st.session_state.pix_atual:
    pix = st.session_state.pix_atual
    nums_str = ", ".join(f"{n:03d}" for n in pix["numeros"])

    st.success(
        f"Pix gerado para os números **{nums_str}** — Total: **R$ {pix['valor_total']:.2f}**"
    )
    if pix.get("detalhe_combos"):
        st.caption(f"Composição: {pix['detalhe_combos']}")

    st.image(base64.b64decode(pix["qr_code_base64"]), caption="Escaneie o QR Code no app do seu banco", width=240)

    st.text_input("Pix Copia e Cola", value=pix["qr_code"])

    col_chk, col_new = st.columns(2)
    with col_chk:
        if st.button("🔄 Já paguei, conferir"):
            try:
                status_mp = consultar_status_pix(pix["payment_id"])
                if status_mp == "approved":
                    marcar_pagamento_como_pago(pix["payment_id"])
                    pix["status_pagamento"] = "approved"
                    st.success("Pagamento aprovado! Seus números agora estão confirmados como PAGO.")
                    st.rerun()
                else:
                    st.info(f"Pagamento ainda em análise/pendente (status: {status_mp}). Aguarde alguns segundos e tente de novo.")
            except Exception as exc:
                st.error(f"Erro ao consultar pagamento: {exc}")

    with col_new:
        if st.button("➕ Escolher mais"):
            st.session_state.pix_atual = None
            st.rerun()
