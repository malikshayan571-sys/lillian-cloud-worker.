import os
import re
import json
import time
import datetime
import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from google import genai

# =========================================================
# CONFIGURATION (LOADED SECURELY FROM CLOUD ENVIRONMENT)
# =========================================================
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
PORT = int(os.environ.get("PORT", 8080))

client = genai.Client(api_key=API_KEY) if API_KEY else None
MODELS = ["gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite-preview"]

seen_email_ids = set()

# =========================================================
# 📧 EMAIL PROCESSING FUNCTIONS
# =========================================================
def clean_email_header(val):
    if not val:
        return ""
    parts = decode_header(val)
    out = ""
    for text_part, enc in parts:
        if isinstance(text_part, bytes):
            out += text_part.decode(enc or 'utf-8', errors='ignore')
        else:
            out += str(text_part)
    return out.strip()

def extract_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get('Content-Disposition'))
            if ctype == 'text/plain' and 'attachment' not in cdispo:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode('utf-8', errors='ignore')
                        break
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode('utf-8', errors='ignore')
        except Exception:
            pass
    return body[:1200].strip()

def send_email_reply(to_address, subject, reply_body):
    """Sends reply email via Gmail SMTP server."""
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_ADDRESS
        msg['To'] = to_address
        if not subject.lower().startswith("re:"):
            msg['Subject'] = f"Re: {subject}"
        else:
            msg['Subject'] = subject

        msg.attach(MIMEText(reply_body, 'plain'))

        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD.replace(" ", ""))
        server.send_message(msg)
        server.quit()
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [EMAIL SENT] Successfully replied to {to_address}")
        return True
    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [SMTP ERROR]: {e}")
        return False

def analyze_incoming_email(sender_name, sender_email, subject, body):
    """Uses Gemini to classify and draft an email response."""
    prompt = f"""
    You are Lillian, an elite autonomous executive AI assistant for Shayan.
    Analyze this incoming email:
    From: {sender_name} <{sender_email}>
    Subject: {subject}
    Body:
    {body}

    Task:
    1. Decide whether this is 'IMPORTANT' or 'ROUTINE'.
       - IMPORTANT: Business contracts, payments, job interviews, urgent requests, high-priority personal decisions, legal matters, or critical questions requiring Shayan's manual decision.
       - ROUTINE: General greetings, promotional/social updates, simple thank-yous, basic meeting confirmations, or non-urgent standard inquiries.
    2. Draft a polite, professional reply written on behalf of Shayan.

    Respond ONLY with valid JSON in this exact structure:
    {{
        "classification": "IMPORTANT" or "ROUTINE",
        "reason": "1 short sentence explaining why",
        "draft_reply": "professional email reply text"
    }}
    """
    for model_name in MODELS:
        try:
            response = client.models.generate_content(model=model_name, contents=prompt)
            raw = response.text
            raw = re.sub(r'```json\s*', '', raw)
            raw = re.sub(r'```\s*', '', raw).strip()
            return json.loads(raw)
        except Exception:
            continue

    return {
        "classification": "IMPORTANT",
        "reason": "Fallback default",
        "draft_reply": "Thank you for reaching out. I have received your email and will follow up shortly."
    }

def fetch_new_unread_emails():
    """Connects to Gmail IMAP and fetches unread emails."""
    emails = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD.replace(" ", ""))
        mail.select("INBOX")

        status, response = mail.search(None, 'UNSEEN')
        if status != "OK" or not response[0]:
            return []

        email_ids = response[0].split()
        for e_id in email_ids[-3:]:
            status, msg_data = mail.fetch(e_id, '(RFC822)')
            if status == "OK":
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject = clean_email_header(msg.get("Subject", "No Subject"))
                        from_header = clean_email_header(msg.get("From", "Unknown Sender"))

                        sender_email = ""
                        email_match = re.search(r'<([^>]+)>', from_header)
                        if email_match:
                            sender_email = email_match.group(1).strip()
                            sender_name = from_header.split("<")[0].replace('"', '').strip()
                        else:
                            sender_email = from_header
                            sender_name = from_header

                        if not sender_name:
                            sender_name = sender_email

                        body = extract_email_body(msg)

                        emails.append({
                            "id": e_id.decode(),
                            "sender": sender_name,
                            "email": sender_email,
                            "subject": subject,
                            "body": body
                        })
        mail.logout()
    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] IMAP sync error: {e}")

    return emails

# =========================================================
# 🔄 24/7 BACKGROUND MONITORING WORKER
# =========================================================
def background_worker_loop():
    global seen_email_ids
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [STARTED] Lillian 24/7 Cloud Worker is online!")
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [MONITORING] Guarding inbox for: {GMAIL_ADDRESS}")

    # Seed existing unread emails so it only processes NEW incoming mail
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD.replace(" ", ""))
        mail.select("INBOX")
        status, response = mail.search(None, 'UNSEEN')
        if status == "OK" and response[0]:
            seen_email_ids.update([e.decode() for e in response[0].split()])
        mail.logout()
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Initialized. {len(seen_email_ids)} existing unread emails logged.")
    except Exception as e:
        print(f"Initial seed error: {e}")

    while True:
        time.sleep(30) # Checks inbox every 30 seconds
        try:
            new_emails = fetch_new_unread_emails()
            for em in new_emails:
                if em['id'] not in seen_email_ids:
                    seen_email_ids.add(em['id'])

                    print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] [NEW EMAIL DETECTED]:")
                    print(f"   From: {em['sender']} ({em['email']})")
                    print(f"   Subject: {em['subject']}")

                    # Analyze with Gemini
                    analysis = analyze_incoming_email(em['sender'], em['email'], em['subject'], em['body'])
                    classification = analysis.get("classification", "IMPORTANT").upper()

                    if classification == "ROUTINE":
                        # Auto-reply immediately
                        draft = analysis.get("draft_reply", "Thank you for reaching out.")
                        if em['email']:
                            send_email_reply(em['email'], em['subject'], draft)
                            print(f"   Action: Routine email auto-replied.")
                    else:
                        # Important email: Flagged for manual review (does not auto-reply without boss)
                        print(f"   Action: IMPORTANT email flagged for Boss ({analysis.get('reason')}).")

        except Exception as e:
            print(f"Worker loop error: {e}")

# =========================================================
# LIGHTWEIGHT HEALTHCHECK HTTP SERVER (KEEPS CLOUD ALIVE)
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        response = {
            "status": "online",
            "service": "Lillian 24/7 AI Cloud Worker",
            "monitoring": GMAIL_ADDRESS,
            "timestamp": datetime.datetime.now().isoformat()
        }
        self.wfile.write(json.dumps(response).encode())

    def log_message(self, format, *args):
        pass # Silence HTTP access logs

def start_http_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [SERVER] Healthcheck endpoint active on port {PORT}")
    server.serve_forever()

if __name__ == "__main__":
    # Start the email worker in a background thread
    worker_thread = threading.Thread(target=background_worker_loop, daemon=True)
    worker_thread.start()

    # Run the HTTP server on main thread to keep container alive 24/7
    start_http_server()
