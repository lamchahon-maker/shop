from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    QuickReply, QuickReplyButton, MessageAction,
    FlexSendMessage
)
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import re

app = Flask(__name__)

# --- 1. ตั้งค่า LINE Channel ---
LINE_CHANNEL_ACCESS_TOKEN = '7ypYraV7f2+fyTlv/NR0Umqo/ZAYESslNWK+UNhr9b5shVZT/bl1KlYaiGb5ubjpZ4C033JjgNeLMn3vRaU796n5LcNIpm5xJnapSuMjrHifh18b2as38cVxlHQVoB5w3YzAKgASqpJ3sD7oJ6M43AdB04t89/1O/w1cDnyilFU='
LINE_CHANNEL_SECRET = '1b22f4db8cd6a919ad5f8ab406f2792f'

# 🔥 User ID ของคุณ (ใส่ให้แล้วครับ)
ADMIN_USER_ID = 'U972f81b73f8a81c124884c68f8d8cbfe' 

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 2. พจนานุกรมคำย่อ ---
SHORTCUTS = {
    "พม": "กระเพราหมู", "พหก": "กระเพราหมูกรอบ", "ตยก": "ต้มยำกุ้ง",
    "ขผ": "ข้าวผัด", "ลน": "ราดหน้า", "ขจ": "ไข่เจียว", "กต": "ก๋วยเตี๋ยว",
    "ก": "เป็นกับข้าว", "ข": "ขอไข่ดาว", "พ": "พิเศษ", "มส": "หมูสับ",
    "บ": "กลับบ้าน", "ร": "กินที่ร้าน"
}

# --- 3. Database จำลอง ---
orders = {}
shop_config = {"is_busy": False} 
billing_sessions = {}

# --- Helper: หาเลขคิวที่ว่างอยู่ ---
def get_next_free_queue():
    q_id = 1
    while q_id in orders:
        q_id += 1
    return q_id

# --- Helper: แปลงคำย่อ ---
def expand_shortcuts(text):
    words = text.split()
    translated = []
    for word in words:
        clean_word = word.replace(",", "") 
        has_comma = "," in word
        translated_word = SHORTCUTS.get(clean_word, clean_word)
        if has_comma:
            translated.append(translated_word + ",")
        else:
            translated.append(translated_word)
    return " ".join(translated)

# --- Helper: จัด Format รายการ ---
def format_order_items(text):
    items = text.split(',')
    formatted_lines = []
    for i, item in enumerate(items, 1):
        clean_item = item.strip()
        if clean_item:
            formatted_lines.append(f"{i}. {clean_item}")
    return "\n".join(formatted_lines)

# --- Helper: คำนวณเวลา ---
def get_thresholds():
    base_prepare = 30 if shop_config["is_busy"] else 15
    base_late = 45 if shop_config["is_busy"] else 30
    return base_prepare, base_late

# --- Helper: Flex Message แสดงออเดอร์เดี่ยว ---
def reply_flex_order(reply_token, q_id, order_data):
    type_text = order_data['type'].replace("#", "")
    if "กลับบ้าน" in type_text:
        header_color = "#06C755"
        badge_color = "#00B900"
        icon = "🏠"
    else:
        header_color = "#FF9800"
        badge_color = "#FF9800"
        icon = "🍽️"

    time_str = order_data['time'].strftime("%H:%M")

    flex_json = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "horizontal",
            "backgroundColor": header_color,
            "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "QUEUE", "color": "#ffffff", "size": "xs", "gravity": "center", "weight": "bold"},
                {"type": "text", "text": f"{q_id}", "color": "#ffffff", "size": "3xl", "weight": "bold", "align": "end", "gravity": "center"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": f"{icon} {type_text}", "weight": "bold", "size": "md", "color": badge_color},
                        {"type": "text", "text": f"🕒 {time_str}", "size": "xs", "color": "#aaaaaa", "align": "end", "gravity": "center"}
                    ]
                },
                {"type": "separator", "margin": "lg"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "lg",
                    "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "รายการอาหาร:", "size": "xs", "color": "#aaaaaa"},
                        {"type": "text", "text": order_data['items'], "size": "lg", "color": "#333333", "wrap": True, "weight": "regular"}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "message", "label": "💰 คิดเงิน", "text": f"คิว {q_id} คิดตังค์"}},
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "message", "label": "✏️ แก้ไข", "text": f"คิว {q_id} แก้ไข"}}
            ]
        }
    }
    line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=f"ออเดอร์คิวที่ {q_id}", contents=flex_json))

# --- Helper ใหม่: Flex Message สรุปรายการ (เรียงตามเวลา) ---
def reply_flex_summary(reply_token, sorted_orders):
    # สร้าง Row สำหรับแต่ละออเดอร์
    rows = []
    for q_id, data in sorted_orders:
        time_str = data['time'].strftime("%H:%M")
        type_text = data['type'].replace("#", "")
        icon = "🏠" if "กลับบ้าน" in type_text else "🍽️"
        # เอาเฉพาะบรรทัดแรกของเมนูมาโชว์ย่อๆ
        first_item = data['items'].split('\n')[0]
        
        row = {
            "type": "box",
            "layout": "vertical",
            "margin": "md",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": f"{icon} คิว {q_id}", "weight": "bold", "size": "sm", "color": "#333333", "flex": 3},
                        {"type": "text", "text": f"🕒 {time_str}", "size": "xs", "color": "#aaaaaa", "align": "end", "flex": 2}
                    ]
                },
                {"type": "text", "text": first_item + "...", "size": "xs", "color": "#666666", "margin": "xs", "maxLines": 1}
            ]
        }
        rows.append(row)
        rows.append({"type": "separator", "margin": "md"}) # เส้นคั่น

    flex_json = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#333333",
            "contents": [
                {"type": "text", "text": "📋 ลำดับคิว (ตามเวลา)", "color": "#ffffff", "weight": "bold", "size": "lg"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": rows
        }
    }
    line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="สรุปลำดับคิว", contents=flex_json))

# --- Background Task ---
def check_order_status():
    now = datetime.now()
    prepare_min, late_min = get_thresholds()
    for q_id, data in list(orders.items()):
        if data.get('status') == 'billing': continue
        elapsed = (now - data['time']).total_seconds() / 60
        if elapsed >= prepare_min and data['alert_step'] == 0:
            msg = f"⚠️ เตือนคิว {q_id}: ผ่านไป {int(elapsed)} นาทีแล้ว จัดเตรียมหรือยัง?"
            push_alert(data['user_id'], msg)
            orders[q_id]['alert_step'] = 1
        elif elapsed >= late_min and data['alert_step'] == 1:
            msg = f"🚨 ล่าช้าคิว {q_id}: ผ่านไป {int(elapsed)} นาทีแล้ว!! (เกินกำหนด)"
            push_alert(data['user_id'], msg)
            orders[q_id]['alert_step'] = 2

def daily_reset_job():
    total_orders = len(orders)
    msg = f"🕛 สรุปยอดประจำวัน\n- รีเซ็ตระบบอัตโนมัติ\n- เคลียร์คิวค้าง: {total_orders} คิว"
    if ADMIN_USER_ID and ADMIN_USER_ID != 'YOUR_ADMIN_ID':
        push_alert(ADMIN_USER_ID, msg)
    orders.clear()
    billing_sessions.clear()

def push_alert(user_id, text):
    try: line_bot_api.push_message(user_id, TextSendMessage(text=text))
    except: pass

scheduler = BackgroundScheduler()
scheduler.add_job(func=check_order_status, trigger="interval", minutes=1)
scheduler.add_job(func=daily_reset_job, trigger='cron', hour=0, minute=0)
scheduler.start()

# --- Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    raw_text = event.message.text.strip()
    user_id = event.source.user_id

    # เช็ค User ID
    if raw_text == "เช็คไอดี":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"User ID ของคุณคือ:\n{user_id}"))
        return
    
    # ถ้าเป็นตัวเลข (คิดเงิน) ไม่ต้องแปลง
    if re.match(r'^[\d\+\s]+$', raw_text) and "คิดตังค์" not in raw_text:
        text = raw_text 
    else:
        text = expand_shortcuts(raw_text)
        
    # ---------------- MODE 1: คิดเงิน ----------------
    if user_id in billing_sessions:
        q_id = billing_sessions[user_id]
        if text in ["ส", "เสร็จ", "เสร็จสิ้น"]:
            if q_id in orders: del orders[q_id] 
            del billing_sessions[user_id]
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ ปิดการขาย คิว {q_id} เรียบร้อย"))
            return
        try:
            price_str = raw_text.replace(" ", "")
            if re.match(r'^[\d\+]+$', price_str):
                total_price = eval(price_str)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"💰 ยอดรวม: {total_price} บาท\n(พิมพ์ 'ส' เพื่อจบงาน)"))
                return
        except: pass
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"กำลังคิดเงินคิว {q_id}... ใส่ราคาหรือพิมพ์ 'ส'"))
        return

    # ---------------- MODE 2: คำสั่งพิเศษ ----------------
    
    # >>> ฟีเจอร์ใหม่: เรียงคิวตามเวลา (Sort) <<<
    if text == "เรียง" or text == "คิวรวม":
        if not orders:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ ร้านว่างมาก ไม่มีออเดอร์ค้างครับ"))
            return
        
        # จัดเรียงตามเวลา (Time)
        sorted_orders = sorted(orders.items(), key=lambda item: item[1]['time'])
        reply_flex_summary(event.reply_token, sorted_orders)
        return

    if text == "ร้านยุ่ง":
        shop_config["is_busy"] = not shop_config["is_busy"]
        status = "🔴 เปิดโหมดร้านยุ่ง (+15 นาที)" if shop_config["is_busy"] else "🟢 ปิดโหมดร้านยุ่ง (เวลาปกติ)"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=status))
        return

    # ขอดูคิวเดี่ยว
    match_view = re.match(r"^คิว\s+(\d+)$", text)
    if match_view:
        q_id = int(match_view.group(1))
        if q_id in orders:
            reply_flex_order(event.reply_token, q_id, orders[q_id])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ ไม่พบข้อมูล คิว {q_id} (ว่างอยู่)"))
        return

    # เพิ่มเมนู
    match_add = re.match(r"คิว\s+(\d+)\s+(?:เพิ่ม|เพิ่มเมนู)\s+(.*)", text)
    if match_add:
        q_id = int(match_add.group(1))
        new_items_raw = match_add.group(2).strip()
        if q_id in orders:
            if "กลับบ้าน" in new_items_raw: orders[q_id]['type'] = "#กลับบ้าน"
            elif "ร้าน" in new_items_raw: orders[q_id]['type'] = "#กินที่ร้าน"
            
            new_items_formatted = format_order_items(new_items_raw)
            orders[q_id]['items'] += f"\n(เพิ่ม) \n{new_items_formatted}" 
            reply_flex_order(event.reply_token, q_id, orders[q_id])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ ไม่พบคิวนี้"))
        return

    # แก้ไขใหม่หมด
    match_change = re.match(r"คิว\s+(\d+)\s+(?:แก้เป็น|เปลี่ยนเป็น)\s+(.*)", text)
    if match_change:
        q_id = int(match_change.group(1))
        new_items_raw = match_change.group(2).strip()
        if q_id in orders:
            if "กลับบ้าน" in new_items_raw: orders[q_id]['type'] = "#กลับบ้าน"
            elif "ร้าน" in new_items_raw: orders[q_id]['type'] = "#กินที่ร้าน"
            
            orders[q_id]['items'] = format_order_items(new_items_raw)
            reply_flex_order(event.reply_token, q_id, orders[q_id])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ ไม่พบคิวนี้"))
        return

    # ปุ่มจัดการแก้ไข
    match_edit_menu = re.match(r"คิว\s+(\d+)\s+แก้ไข$", text)
    if match_edit_menu:
        q_id = int(match_edit_menu.group(1))
        if q_id in orders:
            quick_reply = QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="ลบคิวนี้", text=f"ยืนยันลบคิว {q_id}")),
                QuickReplyButton(action=MessageAction(label="ยกเลิก", text="ยกเลิก"))
            ])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"จัดการคิว {q_id} อย่างไร?", quick_reply=quick_reply))
        return

    # สั่งคิดเงิน
    match_bill = re.match(r"คิว\s+(\d+)\s+คิดตังค์", text)
    if match_bill:
        q_id = int(match_bill.group(1))
        if q_id in orders:
            billing_sessions[user_id] = q_id
            orders[q_id]['status'] = 'billing'
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🧾 คิดเงิน คิว {q_id}\n\n{orders[q_id]['items']}\n\n👉 ใส่ราคาได้เลย (เช่น 50+10)"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ ไม่พบคิวนี้"))
        return

    # ยืนยันลบ
    if text.startswith("ยืนยันลบคิว"):
        try:
            q_id = int(text.split()[1])
            if q_id in orders:
                del orders[q_id]
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ ลบคิว {q_id} แล้ว"))
            return
        except: pass

    # ---------------- MODE 3: รับออเดอร์ใหม่ ----------------
    
    order_type = ""
    if "กลับบ้าน" in text: order_type = "#กลับบ้าน"
    elif "ร้าน" in text or "กินที่ร้าน" in text: order_type = "#กินที่ร้าน"
    
    if order_type:
        current_q = get_next_free_queue()
        
        clean_text = text.replace("กลับบ้าน", "").replace("กินที่ร้าน", "").replace("ร้าน", "")
        formatted_items = format_order_items(clean_text)
        
        orders[current_q] = {
            "items": formatted_items,
            "type": order_type,
            "time": datetime.now(),
            "user_id": user_id,
            "alert_step": 0,
            "status": "cooking"
        }
        reply_flex_order(event.reply_token, current_q, orders[current_q])
        
@app.route('/')
def home():
    return "OK", 200

if __name__ == "__main__":


    app.run(port=5000)
