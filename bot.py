import os
import logging
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
import httpx

# ═══════════════════════════════════════════════════════════
# ENVIRONMENT VARIABLES
# ═══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")

if not all([TELEGRAM_BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, CLAUDE_API_KEY]):
    print("ERROR: Environment variables missing!")
    exit(1)

SUPABASE_URL = SUPABASE_URL.strip().rstrip("/")
if not SUPABASE_URL.startswith("https://"):
    SUPABASE_URL = "https://" + SUPABASE_URL

claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
logging.basicConfig(level=logging.INFO)

# ═══════════════════════════════════════════════════════════
# SUPABASE CLIENT
# ═══════════════════════════════════════════════════════════
class SupabaseClient:
    def __init__(self, url, key):
        self.url = url
        self.key = key
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        self.client = httpx.Client(timeout=10.0)

    def table(self, name):
        self.table_name = name
        return self

    def select(self, *cols):
        self.cols = ",".join(cols) if cols else "*"
        return self

    def eq(self, col, val):
        self.query_col = col
        self.query_val = val
        return self

    def _make_url(self, action):
        base = f"{self.url}/rest/v1/{self.table_name}"
        if action == "get":
            return f"{base}?select={self.cols}&{self.query_col}=eq.{self.query_val}"
        elif action == "patch":
            return f"{base}?{self.query_col}=eq.{self.query_val}"
        return base

    def execute(self):
        url = self._make_url("get")
        r = self.client.get(url, headers=self.headers)
        return type('obj', (object,), {'data': r.json() if r.status_code == 200 else []})()

    def insert(self, data):
        url = self._make_url("insert")
        r = self.client.post(url, json=data, headers=self.headers)
        return type('obj', (object,), {'data': r.json() if r.status_code in [200, 201] else []})()

    def upsert(self, data, on_conflict=None):
        url = self._make_url("upsert")
        headers = {**self.headers, "Prefer": "resolution=merge-duplicates,return=representation"}
        r = self.client.post(url, json=data, headers=headers)
        return type('obj', (object,), {'data': r.json() if r.status_code in [200, 201] else []})()

    def update(self, data):
        self.update_data = data
        return self

    def _apply_update(self):
        url = self._make_url("patch")
        headers = {**self.headers, "Prefer": "return=representation"}
        r = self.client.patch(url, json=self.update_data, headers=headers)
        return type('obj', (object,), {'data': r.json() if r.status_code == 200 else []})()


supabase = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)

# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════
def get_user(telegram_id: int):
    res = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if res.data:
        return res.data[0]
    return None


def upsert_user(telegram_id: int, fields: dict):
    fields["telegram_id"] = telegram_id
    supabase.table("users").upsert(fields, on_conflict="telegram_id").execute()


def log_message(telegram_id: int, role: str, text: str):
    supabase.table("messages").insert({
        "telegram_id": telegram_id,
        "role": role,
        "text": text,
        "created_at": datetime.now(timezone.utc).isoformat()
    }).execute()

# ═══════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════
SYSTEM_PROMPT = """You are Foundertion, an AI co-founder for solo founders who keep stopping at 70-85% of their projects ("the silence", "70-85% graveyard").

Current user context:
- Name: {nama}
- Project: {proyek}
- Why it matters: {why}
- MVP target: {definisi_done}
- Check-in time: {jam_checkin}
- Status: {status}
- Silent days (no reply): {hari_silent}

Behavior guidelines:
1. Reply in the same language as the user (default Bahasa Indonesia).
2. Keep responses to 2-4 sentences. No essays.
3. Always refer to their specific project and "why". No generic motivation.
4. If user is psychologically stuck (fear of launching, perfectionism, overthinking): acknowledge without judgment. Ask: "What is the smallest thing you can work on for 15 minutes today?"
5. If user is technically stuck: ask exactly where they are stuck. Don't give solutions before knowing the details.
6. If user mentions shipping/launching something: celebrate genuinely, then ask: "Who is one person you can share this with today?"
7. If hari_silent >= 2: reach out with empathy. "I noticed you haven't replied in 2 days. No worries — but want to share where you're stuck?"
8. Avoid corporate language. Use community vocabulary: "ship", "the boring middle", "Resistance", "done is better than perfect".
"""

# ═══════════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    first_name = update.effective_user.first_name or "Founder"

    upsert_user(tid, {
        "nama": first_name,
        "status": "q1",
        "proyek": None,
        "why": None,
        "definisi_done": None,
        "jam_checkin": None,
        "hari_silent": 0,
        "last_reply": datetime.now(timezone.utc).isoformat()
    })

    await update.message.reply_text(
        f"Hey {first_name}! I'm Foundertion — your AI co-founder here to make sure you actually ship your idea.\n\n"
        "Let's start. What are you building?"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text
    first_name = update.effective_user.first_name or "Founder"

    user = get_user(tid)

    if not user or not user.get("status"):
        upsert_user(tid, {"nama": first_name, "status": "q1", "hari_silent": 0})
        await update.message.reply_text(
            f"Hey {first_name}! I'm Foundertion — your AI co-founder here to make sure you actually ship your idea.\n\n"
            "Let's start. What are you building?"
        )
        return

    status = user.get("status", "q1")

    # ── ONBOARDING ──
    if status == "q1":
        upsert_user(tid, {"proyek": text, "status": "q2"})
        log_message(tid, "user", text)
        await update.message.reply_text(
            "Got it!\n\nWhy does this matter to you personally?"
        )
        return

    if status == "q2":
        upsert_user(tid, {"why": text, "status": "q3"})
        log_message(tid, "user", text)
        await update.message.reply_text(
            "What's the smallest version you could ship? And what's your target date?"
        )
        return

    if status == "q3":
        upsert_user(tid, {"definisi_done": text, "status": "q4"})
        log_message(tid, "user", text)
        await update.message.reply_text(
            "What time do you usually work on this? (e.g. 21:00)"
        )
        return

    if status == "q4":
        upsert_user(tid, {
            "jam_checkin": text,
            "status": "active",
            "last_reply": datetime.now(timezone.utc).isoformat(),
            "hari_silent": 0
        })
        log_message(tid, "user", text)
        await update.message.reply_text(
            f"Let's go, {user.get('nama', first_name)}! I'll check in with you every day at {text}.\n\n"
            "One last question: in the past, what's usually made you stop building things halfway through?\n\n"
            "(This helps me support you better.)"
        )
        return

    # ── ACTIVE MODE ──
    if status == "active":
        log_message(tid, "user", text)
        upsert_user(tid, {
            "last_reply": datetime.now(timezone.utc).isoformat(),
            "hari_silent": 0
        })

        system = SYSTEM_PROMPT.format(
            nama=user.get("nama", first_name),
            proyek=user.get("proyek", "not filled yet"),
            why=user.get("why", "not filled yet"),
            definisi_done=user.get("definisi_done", "not filled yet"),
            jam_checkin=user.get("jam_checkin", "not filled yet"),
            status=user.get("status", "active"),
            hari_silent=user.get("hari_silent", 0)
        )

        try:
            response = claude.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                system=system,
                messages=[{"role": "user", "content": text}]
            )
            reply = response.content[0].text
        except Exception as e:
            logging.error(f"Claude error: {e}")
            reply = "I'm having a technical issue. But I'm still here — want to share where you're stuck?"

        log_message(tid, "assistant", reply)
        await update.message.reply_text(reply)
        return

    await update.message.reply_text("Something went wrong with your status. Try /start.")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Foundertion bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
