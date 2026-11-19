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
st.set_page_config(page_title="Ação Entre Amigos", page_icon="🎟️", layout="centered")

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
# VARIÁVEIS DE AMBIENTE / CREDENCIAIS
# ==============================================================================
SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL", "")).strip()
SUPABASE_KEY = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")).strip()
MP_ACCESS_TOKEN = st.secrets.get("MP_ACCESS_TOKEN", os.getenv("MP_ACCESS_TOKEN", "")).strip()

RIFA_CONFIG = {
    "titulo": "🏆 Ação Entre Amigos - Prêmio em Dinheiro - Pix de R$ 750,00",
    "descricao": (
        "Escolha seus números favoritos, preencha seus dados e gere o Pix na hora. "
        "Assim que o pagamento é confirmado, seus números ficam garantidos."
    ),
    "regras": (
        "1. Cada número selecionado soma automaticamente no valor total.\n"
        "2. A reserva via Pix tem validade de 15 minutos; após esse prazo, números não pagos voltam a ficar livres.\n"
        "3. Informe um telefone com WhatsApp e e-mail válidos para conferência.\n"
        "4. O sorteio será realizado conforme a data/critério divulgado pelo organizador."
    ),
    "valor_cota": 55.00,
    "total_numeros": 50,
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
# FUNÇÕES DE BANCO (SUPABASE) E MERCADO PAGO
# ==============================================================================
def limpar_reservas_expiradas():
    if not sb:
        return
    agora_iso = datetime.now(timezone.utc).isoformat()
    sb.table("reservas_rifa").delete().eq("status", "pendente").lt("expira_em", agora_iso).execute()


def buscar_ocupados() -> dict[int, str]:
    if not sb:
        return {}
    res = sb.table("reservas_rifa").select("numero, status").execute()
    return {int(row["numero"]): str(row["status"]) for row in (res.data or [])}


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
        "description": f"Rifa - Números: {', '.join(f'{n:02d}' for n in sorted(numeros))}",
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
    valor_total = round(len(numeros_ordenados) * RIFA_CONFIG["valor_cota"], 2)
    expira_em = (datetime.now(timezone.utc) + timedelta(minutes=RIFA_CONFIG["minutos_reserva"])).isoformat()

    # 1. Reserva os números primeiro no Supabase para evitar que dois compradores peguem o mesmo número
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
# VERIFICAÇÃO INICIAL DE CREDENCIAIS
# ==============================================================================
if not SUPABASE_URL or not SUPABASE_KEY or not MP_ACCESS_TOKEN:
    st.warning(
        "Configuração pendente: defina as variáveis de ambiente "
        "`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` e `MP_ACCESS_TOKEN` antes de abrir para os compradores."
    )

try:
    limpar_reservas_expiradas()
    ocupados = buscar_ocupados()
except Exception as exc:
    st.error(f"Erro ao conectar no Supabase: {exc}")
    ocupados = {}

# Remove da seleção local algum número que acabou de ser ocupado por outra pessoa
st.session_state.selecionados = [n for n in st.session_state.selecionados if n not in ocupados]


# ==============================================================================
# INTERFACE PRINCIPAL (DESCRIÇÃO, REGRAS, DADOS, NÚMEROS E PIX)
# ==============================================================================
st.title(RIFA_CONFIG["titulo"])
st.write(RIFA_CONFIG["descricao"])

with st.expander("📜 Regras da Ação", expanded=True):
    st.markdown(RIFA_CONFIG["regras"].replace("\n", "  \n"))

st.caption(
    f"Valor por número: **R$ {RIFA_CONFIG['valor_cota']:.2f}** · "
    f"Total de números: **{RIFA_CONFIG['total_numeros']}** · "
    "🟢 Livre | 🟡 Reservado | 🔴 Pago"
)

st.divider()

# 1. Dados do comprador
st.subheader("1. Seus dados")
nome = st.text_input("Nome completo", placeholder="Ex: Carlos Eduardo")
telefone = st.text_input("Telefone / WhatsApp", placeholder="Ex: 11999998888")
email = st.text_input("E-mail", placeholder="Ex: carlos@email.com")

st.divider()

# 2. Grade de números
st.subheader("2. Escolha seu(s) número(s)")

cols = st.columns(RIFA_CONFIG["colunas"])
for num in range(1, RIFA_CONFIG["total_numeros"] + 1):
    col = cols[(num - 1) % RIFA_CONFIG["colunas"]]
    status = ocupados.get(num)

    if status == "pago":
        col.button(f"🔴 {num:02d}", key=f"num_{num}", disabled=True)
    elif status == "pendente":
        col.button(f"🟡 {num:02d}", key=f"num_{num}", disabled=True)
    else:
        esta_marcado = num in st.session_state.selecionados
        rotulo = f"🟢 {num:02d} ✓" if esta_marcado else f"🟢 {num:02d}"
        if col.button(rotulo, key=f"num_{num}"):
            if esta_marcado:
                st.session_state.selecionados.remove(num)
            else:
                st.session_state.selecionados.append(num)
            st.rerun()

# 3. Soma e geração do Pix
qtd_selecionados = len(st.session_state.selecionados)
soma_total = round(qtd_selecionados * RIFA_CONFIG["valor_cota"], 2)

st.divider()
st.subheader("3. Resumo e Pagamento Pix")

if qtd_selecionados > 0:
    numeros_fmt = ", ".join(f"{n:02d}" for n in sorted(st.session_state.selecionados))
    st.info(
        f"**Números escolhidos ({qtd_selecionados}):** {numeros_fmt}  \n"
        f"**Valor total:** **R$ {soma_total:.2f}**"
    )
else:
    st.caption("Selecione um ou mais números verdes acima para calcular o valor total.")

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
    nums_str = ", ".join(f"{n:02d}" for n in pix["numeros"])

    if pix.get("status_pagamento") == "approved":
        st.success(
            f"✅ Pagamento aprovado! Os números **{nums_str}** (R$ {pix['valor_total']:.2f}) já estão garantidos como PAGO."
        )
        if st.button("➕ Escolher mais números"):
            st.session_state.pix_atual = None
            st.rerun()
    else:
        st.info(
            f"Pix gerado para os números **{nums_str}** — Total: **R$ {pix['valor_total']:.2f}**"
        )
        st.image(base64.b64decode(pix["qr_code_base64"]), caption="Escaneie o QR Code no app do seu banco", width=240)
        st.code(pix["qr_code"], language=None)

        col_chk, col_new = st.columns(2)
        with col_chk:
            if st.button("🔄 Já paguei, conferir"):
                try:
                    status_mp = consultar_status_pix(pix["payment_id"])
                    if status_mp == "approved":
                        marcar_pagamento_como_pago(pix["payment_id"])
                        pix["status_pagamento"] = "approved"
                        st.rerun()
                    else:
                        st.warning(f"Pagamento ainda pendente (status: {status_mp}). Aguarde alguns segundos e clique novamente.")
                except Exception as exc:
                    st.error(f"Erro ao consultar pagamento: {exc}")

        with col_new:
            if st.button("➕ Escolher mais"):
                st.session_state.pix_atual = None
                st.rerun()                
            else:
                    st.info(f"Pagamento ainda em análise/pendente (status: {status_mp}). Aguarde alguns segundos e tente de novo.")
            except Exception as exc:
                st.error(f"Erro ao consultar pagamento: {exc}")

    with col_new:
        if st.button("➕ Escolher mais"):
            st.session_state.pix_atual = None
            st.rerun()
