import io
import uuid
import qrcode
import logging
import os
from fastapi import FastAPI, Request, BackgroundTasks
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import httpx

# — Carrega variáveis de ambiente —
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
PIX_KEY        = os.environ['PIX_KEY']
MP_TOKEN       = os.environ['MP_ACCESS_TOKEN']

# — Define produtos e estoque simples —
PRODUCTS = {
    "Rotativa 1GB": 1000,
    "Rotativa 2GB": 1950,
    "Rotativa 5GB": 4700,
}
STOCK = {k: 10 for k in PRODUCTS}
PENDING = {}  # order_id -> (chat_id, produto, qty)

# — Inicializa FastAPI e Telegram Bot —
app = FastAPI()
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# — Função que gera payload Pix (incluindo CRC16 e nosso txid) —
def build_pix_payload(key, cents, name, city, txid):
    def tlv(i, v): return f"{i}{len(v):02d}{v}"
    p = tlv("00","01")
    p += tlv("26", tlv("00","BR.GOV.BCB.PIX") + tlv("01",key))
    p += tlv("27", tlv("01",txid))
    p += tlv("52","0000") + tlv("53","986") + tlv("54",f"{cents/100:.2f}")
    p += tlv("58","BR") + tlv("59",name[:25]) + tlv("60",city[:15])
    # calcula CRC16
    poly, reg = 0x1021, 0xFFFF
    for c in (p + "6304"):
        reg ^= ord(c) << 8
        for _ in range(8):
            reg = (reg << 1) ^ poly if reg & 0x8000 else reg << 1
            reg &= 0xFFFF
    checksum = f"{reg:04X}"
    return p + tlv("63", checksum)

# — Handlers do Telegram —
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton(f"{p} — R$ {v/100:.2f}", callback_data=f"buy|{p}")]
        for p, v in PRODUCTS.items()
    ]
    await update.message.reply_text("Escolha um plano de proxy:", reply_markup=InlineKeyboardMarkup(buttons))

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    _, prod = update.callback_query.data.split("|", 1)
    await update.callback_query.message.reply_text(f"{prod}: {STOCK[prod]} disponíveis. Quantas deseja?")
    ctx.user_data['prod'] = prod
    ctx.user_data['await_qty'] = True

async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.pop('await_qty', False):
        try:
            qty = int(update.message.text.strip())
        except ValueError:
            return await update.message.reply_text("Envie um número inteiro.")
        prod = ctx.user_data['prod']
        if qty < 1 or qty > STOCK[prod]:
            return await update.message.reply_text(f"Mínimo 1 / Máximo {STOCK[prod]}")
        STOCK[prod] -= qty
        order_id = uuid.uuid4().hex[:8]
        PENDING[order_id] = (update.effective_chat.id, prod, qty)
        total_cents = PRODUCTS[prod] * qty
        payload = build_pix_payload(PIX_KEY, total_cents, "ProxyBat", "SAO PAULO", order_id)
        img = qrcode.make(payload)
        buf = io.BytesIO(); buf.name = 'pix.png'; img.save(buf, 'PNG'); buf.seek(0)

        buttons = [[
            InlineKeyboardButton(
                "Copiar código Pix",
                switch_inline_query_current_chat=payload
            )
        ]]
        await update.message.reply_photo(
            buf,
            caption=(
                f"Pedido {order_id}: {qty}×{prod} — Total R$ {total_cents/100:.2f}\n"
                "Pague via Pix. Você receberá o proxy após confirmação."
            ),
            reply_markup=InlineKeyboardMarkup(buttons)
        )

# — Endpoint de webhook Mercado Pago —
@app.post("/mercadopago/webhook")
async def mp_webhook(request: Request, bg: BackgroundTasks):
    data = await request.json()
    # processa apenas notificações de pagamento
    if data.get('topic') != 'payment':
        return {}
    notif_id = data.get('id')
    # consulta status do pagamento
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.mercadopago.com/v1/payments/{notif_id}",
            headers={"Authorization": f"Bearer {MP_TOKEN}"}
        )
    pay = r.json()
    oid = pay.get('external_reference')
    if pay.get('status') == 'approved' and oid in PENDING:
        chat_id, prod, qty = PENDING.pop(oid)
        def send_proxy():
            with open('proxy.txt', 'rb') as f:
                bot_app.bot.send_document(
                    chat_id,
                    InputFile(f, filename='proxies.txt'),
                    caption="✅ Pagamento confirmado! Aqui estão seus proxies."
                )
        bg.add_task(send_proxy)
    return {}

# — Inicialização —
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # inicia o bot em polling em background
    bot_app.run_polling()
