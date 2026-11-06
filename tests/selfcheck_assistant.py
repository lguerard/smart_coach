#!/usr/bin/env python3
"""Self-check for the assistant: every tool on a seeded account, the
scoping between two accounts, the writes and their undo, the tool loop
against a fake Ollama, and the web endpoints.

No model is ever called: Ollama is an ``httpx.MockTransport`` that
plays a scripted conversation and records what it was sent.
"""

import asyncio
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ["SMART_COACH_DB"] = str(TMP / "assistant.db")
os.environ["ASSISTANT_OLLAMA_URL"] = ""
os.environ.pop("ASSISTANT_NUM_CTX", None)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import httpx  # noqa: E402

import assistant  # noqa: E402
import db  # noqa: E402
import done_sessions  # noqa: E402
import gcal  # noqa: E402
import metrics  # noqa: E402
import sleep_feedback  # noqa: E402
import training  # noqa: E402
import weather  # noqa: E402
from _seed_assistant import seed  # noqa: E402

conn = db.connect()
db.init_db(conn)
A = db.create_user(conn, "alice", "password-a1", approved=True)
B = db.create_user(conn, "bob", "password-b1", approved=True)
TODAY = dt.datetime.now(metrics.local_tz(conn, A)).date().isoformat()
seed(conn, A, TODAY, 1.0, "a")
seed(conn, B, TODAY, 1.37, "b")
# Today is a training day for both, whatever the weekday.
for uid in (A, B):
    conn.execute("DELETE FROM coach_log WHERE user_id = ? AND local_date = ?",
                 (uid, TODAY))
    conn.execute(
        "INSERT INTO coach_log (user_id, created_at, local_date, status, "
        "session_type, level, message, tier) VALUES (?, 'x', ?, 'yellow', "
        "'lower_body', 4, 'Message du jour', 'train')", (uid, TODAY))
    training.set_level(conn, uid, "lower_body", 4)
conn.commit()
gcal.push_description = lambda *a, **k: None
weather.today_weather = lambda city, tz="", fetch=None: {
    "temp_max_c": 14.5, "precip_mm": 0.0, "condition_fr": "plutot degage"}
db.set_setting(conn, A, "city", "Lyon")


def call(uid, name, args=None, undo=False):
    user = "alice" if uid == A else "bob"
    result = assistant.run_tool(uid, user, name, args or {}, undo=undo)
    assert len(result["result"]) <= assistant.RESULT_CAP, (name, len(result["result"]))
    json.loads(result["result"])  # always valid JSON for the model
    return result


# --- 1. Every read tool, every domain, on a seeded account -----------------
READ_CALLS = [
    ("bilan", {}), ("aujourdhui", {}), ("sommeil", {"jours": 7}),
    ("seances", {"jours": 14}), ("programme", {}), ("repas", {}),
    ("entrainements", {"jours": 14}), ("entrainements", {"jour": "hier"}),
    ("coeur", {"jours": 14}),
    *[("donnees_brutes", {"table": t, "du": "2026-01-01"})
      for t in assistant.RAW_TABLES],
    ("repas", {"jour": "hier"}), ("progres", {}),
    *[("tendances", {"metrique": m, "jours": 30}) for m in assistant.METRICS],
    *[("donnees", {"domaine": d, "jours": 14}) for d in assistant.DOMAINS],
]
outputs = {A: {}, B: {}}
for uid in (A, B):
    for name, args in READ_CALLS:
        result = call(uid, name, args)
        assert result["ok"] and not result["write"], (name, args, result["result"])
        outputs[uid][(name, json.dumps(args))] = result["result"]
# Every read tool and every domain was exercised.
assert {n for n, _ in READ_CALLS} == set(assistant.READERS)

out_a = {k: json.loads(v) for k, v in outputs[A].items()}
today_a = out_a[("aujourdhui", "{}")]
assert today_a["statut"] == "yellow" and today_a["seance_du_soir"]["niveau"] == 4
assert today_a["signaux"] and "seuils" in today_a, today_a
assert "reste" in today_a["budget"]
sleep_a = out_a[("sommeil", '{"jours": 7}')]
assert sleep_a["nuits"] and sleep_a["nuits"][0]["date"] == TODAY, sleep_a
assert any(n.get("ressenti") == "bad" for n in sleep_a["nuits"])
weight_a = out_a[("tendances", '{"metrique": "poids", "jours": 30}')]
assert weight_a["direction"] == "baisse" and weight_a["min"] < 80, weight_a
steps_a = out_a[("tendances", '{"metrique": "pas", "jours": 30}')]
assert steps_a["objectif"] == 10000 and steps_a["jours_avec_donnee"] >= 30
sessions_a = out_a[("seances", '{"jours": 14}')]
assert sessions_a["compte"].get("faite") and sessions_a["compte"].get("manquee"), sessions_a
meals_a = out_a[("repas", '{"jour": "hier"}')]
assert meals_a["total"]["calories_kcal"] == 1900, meals_a
assert {m["meal"] for m in meals_a["par_repas"]} == {"petit-dejeuner", "dejeuner", "diner"}
prog_a = out_a[("programme", "{}")]
assert "regles" in prog_a and prog_a["seances"]["lower_body"]["niveau"] == 4
body_a = out_a[("donnees", '{"domaine": "corps", "jours": 14}')]
assert body_a["tour_de_taille"]["latest_cm"] == 90.5, body_a
assert "photo" not in json.dumps(body_a).replace("les photos du Corps", "")
assert out_a[("donnees", '{"domaine": "meteo", "jours": 14}')]["aujourdhui"]["temp_max_c"] == 14.5
assert out_a[("donnees", '{"domaine": "trophees", "jours": 14}')]["derniers"]
messages_a = out_a[("donnees", '{"domaine": "messages", "jours": 14}')]
assert "Bonjour a" in json.dumps(messages_a, ensure_ascii=False)
settings_a = out_a[("donnees", '{"domaine": "reglages", "jours": 14}')]["reglages"]
assert settings_a["step_goal"] == "10000"
for secret in ("ntfy_topic", "rclone_remote", "calendar_name", "password"):
    assert secret not in settings_a, secret
assert out_a[("donnees", '{"domaine": "synchro", "jours": 14}')]["garmin"]
assert out_a[("bilan", "{}")]["session"], out_a[("bilan", "{}")]

# Workouts with their heart rate, and the heart on its own.
workouts_a = out_a[("entrainements", '{"jours": 14}')]
first_w = workouts_a["entrainements"][0]
assert first_w["fc_moyenne"] and first_w["fc_max"] and first_w["temps_par_zone_pct"], first_w
assert workouts_a["fc_max_estimee"] == 180 and workouts_a["zones"][0]["a_partir_de_bpm"] == 108
one_day = out_a[("entrainements", '{"jour": "hier"}')]
assert isinstance(one_day["entrainements"], str) or \
    one_day["entrainements"][0]["courbe_fc_par_5_min"], one_day
# The sessions list carries what the watch recorded, heart rate included.
done_day = next(r for r in sessions_a["par_jour"] if r.get("enregistre_par_la_montre"))
assert done_day["enregistre_par_la_montre"][0]["fc_moyenne"], done_day
heart_a = out_a[("coeur", '{"jours": 14}')]
assert heart_a["fc_repos"]["base_personnelle"] and heart_a["vfc"][0]["nuit_ms"], heart_a
assert heart_a["journee"][0]["max"] == 150 and heart_a["pendant_les_entrainements"], heart_a
# Any table, row by row; secrets and the Corps photos are not tables it reads.
raw_weight = out_a[("donnees_brutes", '{"table": "poids", "du": "2026-01-01"}')]
assert raw_weight["colonnes"] == ["time_utc", "local_date", "kg"], raw_weight
assert raw_weight["lignes_dans_la_periode"] == 31 and len(raw_weight["lignes"]) == 30
for hidden in ("users", "passkeys", "settings", "body_photos"):
    assert hidden not in {t for t, *_ in assistant.RAW_TABLES.values()}, hidden
assert not call(A, "donnees_brutes", {"table": "users"})["ok"]
# A domain asked for in other words lands on the right tool.
assert "fc_repos" in json.loads(call(A, "donnees", {"domaine": "coeur"})["result"])
assert "entrainements" in json.loads(call(A, "donnees", {"domaine": "workouts"})["result"])

# --- 2. Scoping: A's tools never show B's figures, and back ----------------
blob_a = " ".join(outputs[A].values())
blob_b = " ".join(outputs[B].values())
for marker in ("poulet b", "Bonjour b", "Message du matin b"):
    assert marker not in blob_a, marker
for marker in ("poulet a", "Bonjour a", "Message du matin a"):
    assert marker not in blob_b, marker
weight_b = json.loads(outputs[B][("tendances", '{"metrique": "poids", "jours": 30}')])
assert weight_b["min"] > 100 > weight_a["max"], (weight_a, weight_b)
# The model cannot name another account: identity comes from the session.
sneaky = call(A, "sommeil", {"jours": 7, "user_id": B})
assert "Bonjour b" not in sneaky["result"]
assert json.loads(sneaky["result"])["nuits"][0]["score_montre"] == \
    sleep_a["nuits"][0]["score_montre"]

# --- 3. Argument validation: errors go back to the model --------------------
for name, args in (("tendances", {"metrique": "humeur"}), ("tendances", {}),
                   ("donnees", {"domaine": "photos"}),
                   ("repas", {"jour": "demain"}), ("repas", {"jour": "1999-01-01"}),
                   ("nope", {})):
    result = call(A, name, args)
    assert not result["ok"] and "error" in json.loads(result["result"]), (name, result)
# Windows are bounded instead of refused.
assert json.loads(call(A, "sommeil", {"jours": 400})["result"])["jours"] == 14
assert json.loads(call(A, "seances", {"jours": "3"})["result"])["jours"] == 3

# --- 4. Writes: same code path as the page, undo puts it back --------------
first = call(A, "noter_nuit", {"ressenti": "bad"})
assert first["ok"] and first["write"] and first["undo"] is None
second = call(A, "noter_nuit", {"ressenti": "good"})
assert sleep_feedback.felt(conn, A, TODAY) == "good"
assert second["undo"] == {"tool": "noter_nuit", "args": {"ressenti": "bad"}}
assert call(A, **{"name": second["undo"]["tool"], "args": second["undo"]["args"]},
            undo=True)["ok"]
assert sleep_feedback.felt(conn, A, TODAY) == "bad"
assert sleep_feedback.felt(conn, B, TODAY) is None  # B untouched
assert not call(A, "noter_nuit", {"ressenti": "super"})["ok"]

level = call(A, "regler_niveau", {"niveau": 7})
assert level["ok"] and training.get_level(conn, A, "lower_body") == 7
assert training.get_level(conn, B, "lower_body") == 4
assert level["undo"] == {"tool": "regler_niveau", "args": {"niveau": 4}}
call(A, "regler_niveau", level["undo"]["args"], undo=True)
assert training.get_level(conn, A, "lower_body") == 4
assert not call(A, "regler_niveau", {"niveau": 11})["ok"]
assert not call(A, "regler_niveau", {"niveau": "max"})["ok"]
conn.execute("UPDATE coach_log SET tier = 'rest' WHERE user_id = ? AND local_date = ?",
             (B, TODAY))
conn.commit()
refused = call(B, "regler_niveau", {"niveau": 6})
assert not refused["ok"] and training.get_level(conn, B, "lower_body") == 4

sick = call(A, "declarer_malade", {"jours": 2})
assert training.sick_until(conn, A) == (
    dt.date.fromisoformat(TODAY) + dt.timedelta(days=1)).isoformat()
assert sick["undo"] == {"tool": "declarer_malade", "args": {"jours": 0}}
call(A, "declarer_malade", sick["undo"]["args"], undo=True)
assert not training.sick_until(conn, A)
assert not call(A, "declarer_malade", {"jours": 30})["ok"]

yesterday = (dt.date.fromisoformat(TODAY) - dt.timedelta(days=1)).isoformat()
done = call(A, "declarer_seance", {"jour": "hier", "seance": done_sessions.NOTHING})
assert done["ok"] and done_sessions.done_on(conn, A, yesterday)["session_type"] == "none"
assert done["undo"]["args"] == {"jour": yesterday, "seance": "planned"}
# "planned" (which clears the declaration) is the undo's alone.
assert not call(A, "declarer_seance", {"jour": "hier", "seance": "planned"})["ok"]
call(A, "declarer_seance", done["undo"]["args"], undo=True)
known = done_sessions.done_on(conn, A, yesterday)
assert known is None or known["source"] != "declared", known
assert not call(A, "declarer_seance", {"jour": "2001-01-01", "seance": "treadmill"})["ok"]

goal = call(A, "regler_objectif", {"objectif": "step_goal", "valeur": 12000})
assert db.get_setting(conn, A, "step_goal") == "12000"
assert db.get_setting(conn, B, "step_goal") == "10000"
call(A, "regler_objectif", goal["undo"]["args"], undo=True)
assert db.get_setting(conn, A, "step_goal") == "10000"
assert not call(A, "regler_objectif", {"objectif": "step_goal", "valeur": 5})["ok"]
assert not call(A, "regler_objectif", {"objectif": "ntfy_topic", "valeur": 1})["ok"]
weight_goal = call(A, "regler_objectif", {"objectif": "weekly_weight_change_kg",
                                          "valeur": "-0.5"})
assert weight_goal["ok"] and db.get_setting(conn, A, "weekly_weight_change_kg") == "-0.5"
# "Mets-moi a 1900 kcal par jour": a fixed calorie target, really set,
# with the computed one back on undo or on 0.
import progress  # noqa: E402

kcal = call(A, "regler_objectif", {"objectif": "calorie_target_fixed_kcal",
                                   "valeur": 1900})
assert kcal["ok"] and progress.macro_targets(conn, A, TODAY)["calorie_target_kcal"] == 1900
assert progress.macro_targets(conn, B, TODAY).get("calorie_source") != "fixed"
call(A, "regler_objectif", kcal["undo"]["args"], undo=True)
assert progress.macro_targets(conn, A, TODAY).get("calorie_source") != "fixed"
call(A, "regler_objectif", {"objectif": "calorie_target_fixed_kcal", "valeur": 1900})
assert call(A, "regler_objectif", {"objectif": "calorie_target_fixed_kcal",
                                   "valeur": 0})["ok"]
assert db.get_setting(conn, A, "calorie_target_fixed_kcal") == ""
assert not call(A, "regler_objectif", {"objectif": "calorie_target_fixed_kcal",
                                       "valeur": 600})["ok"]


# --- 5. The tool loop against a fake Ollama ---------------------------------
def ndjson(*chunks):
    return "".join(json.dumps(c) + "\n" for c in chunks)


def fake_ollama(script, seen, delay=0.0):
    """Answers each /api/chat call with the next scripted reply."""
    async def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append(body)
        if delay:
            await asyncio.sleep(delay)
        reply = script[min(len(seen) - 1, len(script) - 1)]
        if isinstance(reply, int):
            return httpx.Response(reply, text="boom")
        return httpx.Response(200, text=ndjson(*reply))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def tool_call(name, args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done": True}


def words(*pieces):
    return [*({"message": {"role": "assistant", "content": p}, "done": False}
              for p in pieces), {"message": {"content": ""}, "done": True}]


async def collect(uid, history, script, delay=0.0):
    seen = []
    events = [e async for e in assistant.chat_events(
        uid, "alice" if uid == A else "bob", history,
        client=fake_ollama(script, seen, delay))]
    return events, seen


assistant.OLLAMA_URL = "http://ollama:11434"
question = [{"role": "user", "content": "Pourquoi je suis en jaune ?"}]
events, seen = asyncio.run(collect(A, question, [
    [tool_call("aujourdhui", {}), ],
    [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "sommeil", "arguments": '{"jours": "3"}'}},
        {"function": {"name": "tendances", "arguments": {"metrique": "humeur"}}}]},
      "done": True}],
    words("Ta nuit ", "a été **courte**."),
]))
kinds = [e["type"] for e in events]
assert kinds[0] == "status" and kinds[-1] == "done", kinds
assert [e["name"] for e in events if e["type"] == "tool"] == [
    "aujourdhui", "sommeil", "tendances"]
cards = [e for e in events if e["type"] == "card"]
assert [c["ok"] for c in cards] == [True, True, False], cards
assert "".join(e["text"] for e in events if e["type"] == "delta") == \
    "Ta nuit a été **courte**."
assert len(seen) == 3
for body in seen:  # what every request to the shared Ollama carries
    assert body["think"] is False and body["stream"] is True
    assert body["options"]["num_ctx"] == 8192 and body["model"] == "gemma4:12b"
    assert body["tools"] and body["messages"][0]["role"] == "system"
assert "tutoies" in seen[0]["messages"][0]["content"]
tool_msgs = [m for m in seen[2]["messages"] if m["role"] == "tool"]
assert [m["tool_name"] for m in tool_msgs] == ["aujourdhui", "sommeil", "tendances"]
assert '"statut":"yellow"' in tool_msgs[0]["content"]
assert '"jours":3' in tool_msgs[1]["content"] and "error" in tool_msgs[2]["content"]
assert all(len(m["content"]) <= assistant.RESULT_CAP for m in tool_msgs)

# A write in the loop becomes an action event with its undo.
events, seen = asyncio.run(collect(A, [{"role": "user", "content": "J'ai bien dormi, note-le"}], [
    [tool_call("noter_nuit", {"ressenti": "good"})], words("C'est noté !")]))
action = next(e for e in events if e["type"] == "action")
assert action["ok"] and action["undo"] == {"tool": "noter_nuit", "args": {"ressenti": "bad"}}
assert "bien dormi" in action["label"]

# At most five rounds, the last one without tools, and it must answer.
events, seen = asyncio.run(collect(A, question, [[tool_call("programme", {})]]))
assert len(seen) == assistant.MAX_ROUNDS
assert all("tools" in b for b in seen[:-1]) and "tools" not in seen[-1]
assert "Dernier tour" in seen[-1]["messages"][-1]["content"]
assert events[-2]["type"] == "error" and events[-1]["type"] == "done"

# Scoping through the loop: B's question gets B's data only.
events, seen = asyncio.run(collect(B, question, [[tool_call("repas", {})], words("ok")]))
tool_text = json.dumps([m for m in seen[1]["messages"] if m["role"] == "tool"],
                       ensure_ascii=False)
assert "poulet b" in tool_text and "poulet a" not in tool_text

# Ollama down, and the deadline: a clean error in the user's language.
events, _ = asyncio.run(collect(A, question, [500]))
assert events[-2] == {"type": "error", "text": assistant.text("fr", "unreachable")}
assistant.DEADLINE_SEC = 0.3
events, _ = asyncio.run(collect(A, question, [words("trop tard")], delay=1.0))
assert events[-2] == {"type": "error", "text": assistant.text("fr", "deadline")}, events
db.set_setting(conn, A, "language", "en")
events, _ = asyncio.run(collect(A, question, [words("late")], delay=1.0))
assert events[-2]["text"] == assistant.text("en", "deadline")
assert "Thinking" in events[0]["text"]
db.set_setting(conn, A, "language", "fr")
assistant.DEADLINE_SEC = 180

# A long conversation is trimmed to the context; the question stays.
long_history = [{"role": r, "content": "z" * 6000}
                for _ in range(10) for r in ("user", "assistant")] + question
events, seen = asyncio.run(collect(A, long_history, [words("ok")]))
sent = seen[0]["messages"]
assert sent[-1]["content"] == question[0]["content"] and len(sent) < len(long_history)
assert assistant.tokens(sent) + assistant.tokens(seen[0]["tools"]) <= \
    assistant.budget_tokens()

# --- 6. The web endpoints ----------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from web import app as web_app  # noqa: E402

assistant.OLLAMA_URL = ""
web = TestClient(web_app.app)
anon = web.post("/assistant/chat", json={"messages": question})
assert anon.status_code == 401, anon.status_code  # JSON, not a login redirect
assert web.post("/login", data={"username": "alice", "password": "password-a1"},
                follow_redirects=False).status_code == 303
page = web.get("/")
assert page.status_code == 200 and 'id="sc-chat"' in page.text
assert f"smartcoach.assistant.{A}" in page.text
assert "ASSISTANT_OLLAMA_URL" in page.text  # the disabled panel says why
assert "Demander au coach" in page.text
disabled = web.post("/assistant/chat", json={"messages": question})
assert disabled.status_code == 503 and "ASSISTANT_OLLAMA_URL" in disabled.json()["error"]

assistant.OLLAMA_URL = "http://ollama:11434"
assert web.post("/assistant/chat", data={"messages": "x"}).status_code == 415
assert web.post("/assistant/chat", json={"messages": []}).status_code == 400
too_long = web.post("/assistant/chat", json={"messages": [
    {"role": "user", "content": "x" * (assistant.MAX_USER_CHARS + 1)}]})
assert too_long.status_code == 413 and "trop long" in too_long.json()["error"]
huge = web.post("/assistant/chat", content=b'{"messages": "' + b"x" * 300_000 + b'"}',
                headers={"content-type": "application/json"})
assert huge.status_code == 413
assert assistant.try_acquire(A)
busy = web.post("/assistant/chat", json={"messages": question})
assert busy.status_code == 429 and "déjà en cours" in busy.json()["error"]
assistant.release(A)

web_seen = []
assistant.make_client = lambda: fake_ollama(
    [[tool_call("repas", {})], words("Il te reste ", "des protéines.")], web_seen)
with web.stream("POST", "/assistant/chat", json={"messages": question}) as response:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    streamed = [json.loads(line) for line in response.iter_lines() if line.strip()]
assert streamed[-1] == {"type": "done"}
assert "".join(e["text"] for e in streamed if e["type"] == "delta") == \
    "Il te reste des protéines."
assert any(e["type"] == "card" and e["tool"] == "repas" for e in streamed)
tool_text = json.dumps(web_seen[1]["messages"], ensure_ascii=False)
assert "poulet a" in tool_text and "poulet b" not in tool_text
assert assistant.try_acquire(A)  # the slot was released
assistant.release(A)

# Undo: writes only, this account only.
assert web.post("/assistant/undo", json={"tool": "bilan", "args": {}}).status_code == 400
assert web.post("/assistant/undo", json={"tool": "noter_nuit",
                                         "args": {"ressenti": "nope"}}).status_code == 400
undone = web.post("/assistant/undo", json={"tool": "noter_nuit",
                                           "args": {"ressenti": "ok"}})
assert undone.status_code == 200 and undone.json()["ok"], undone.text
assert sleep_feedback.felt(conn, A, TODAY) == "ok"
assert sleep_feedback.felt(conn, B, TODAY) is None
# Signed out: the next page clears the browser's conversations.
web.post("/logout")
login_page = web.get("/login").text
assert "smartcoach.assistant." in login_page and 'id="sc-chat"' not in login_page

# --- 7. The panel's Markdown: escaped first, no raw HTML, no javascript: ---
node = shutil.which("node")
if node:
    template = (ROOT / "web/templates/_assistant.html").read_text()
    start = template.index("  function esc(s)")
    end = template.index("  window.__scMarkdown")
    script = template[start:end] + """
const cases = [
  ['<img src=x onerror=alert(1)>', (h) => !h.includes('<img') && h.includes('&lt;img')],
  ['[x](javascript:alert(1))', (h) => !h.includes('href')],
  ['[x](https://ok.example/a?b=1&c=2)', (h) => h.includes('href="https://ok.example/a?b=1&amp;c=2"')],
  ['**gras** et `code`', (h) => h.includes('<strong>gras</strong>') && h.includes('<code>code</code>')],
  ['- un\\n- deux', (h) => h.includes('<ul><li>un</li><li>deux</li></ul>')],
  ['"><script>alert(1)</script>', (h) => !h.includes('<script')],
  ['[a](https://x.example/"onmouseover=alert(1))', (h) => !/href="[^"]*"on/.test(h)],
];
for (const [src, ok] of cases) {
  const html = md(src);
  if (!ok(html)) { console.error('FAIL', src, html); process.exit(1); }
}
console.log('markdown ok');
"""
    run = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    print("panel markdown: " + run.stdout.strip())
else:
    print("panel markdown: node not installed, skipped")

print("selfcheck_assistant.py: all checks passed (no live model call made)")
