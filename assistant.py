#!/usr/bin/env python3
"""The in-app assistant: ask the coach about your own data, and decide.

The morning message is written by Claude; this chat is not. It runs on
the LOCAL model -- one Ollama on the home server, shared with other
apps -- so a question about a night, a weight trend or tonight's
session never sends health data off the machine.

Same split as the rest of the project: code computes and decides, the
model writes. The model gets tools, never the database. Every tool is
a thin, read-mostly wrapper over the functions the pages and the
morning run already use (``coach_payload``, ``coach_brief``,
``training``, ``progress``, ``sleep_analysis``...), scoped to the
session's ``user_id``, and returns a compact JSON already reduced to
what matters (min/max/average/direction, never raw rows). The model is
told to quote those figures and nothing else, and to explain the
decisions the code made rather than redo them.

Writes are limited to the edits the dashboard already offers, through
the very same functions (``day_actions``, ``sleep_feedback``,
``training.set_sick``, ``db.set_setting`` for a goal); each reports an
``action`` event with an undo that puts the previous value back.
Nothing is ever deleted.

Protocol: ``chat_events`` is an async generator of events the web
route streams as NDJSON -- ``status`` / ``tool`` / ``card`` /
``action`` / ``delta`` / ``error`` / ``done`` (see web/app.py).

Configuration (environment):
    ASSISTANT_OLLAMA_URL: e.g. ``http://ollama:11434``; empty = the chat
        is disabled and says so.
    ASSISTANT_MODEL: default ``gemma4:12b``.
    ASSISTANT_NUM_CTX: default 8192 -- see NUM_CTX below.
"""

import asyncio
import datetime as dt
import json
import os
import re
import sqlite3
from typing import Any, AsyncIterator, Callable, Optional

import aids
import body
import coach_brief
import coach_payload
import day_actions
import db
import day_review
import done_sessions
import meals
import missed_sessions
import metrics
import muscles
import progress
import sent_log
import sleep_analysis
import sleep_feedback
import training
import training_load
import waist

OLLAMA_URL = os.environ.get("ASSISTANT_OLLAMA_URL", "").strip().rstrip("/")
MODEL = os.environ.get("ASSISTANT_MODEL", "").strip() or "gemma4:12b"
# MUST stay equal to the num_ctx every other app sends to the shared
# Ollama: a request with a different num_ctx makes Ollama reload the
# model with that context size, evicting it for everyone else (a cold
# load is ~10 s on that GPU, and the next app's request reloads it again).
NUM_CTX = int(os.environ.get("ASSISTANT_NUM_CTX", "") or 8192)

MAX_ROUNDS = 5           # model calls per question; the last one has no tools
DEADLINE_SEC = 180       # whole answer, queueing behind other jobs included
RESULT_CAP = 2500        # chars per tool result handed back to the model
CHARS_PER_TOKEN = 4      # rough estimate, on purpose: no tokenizer here
REPLY_TOKENS = 900       # kept free for the answer itself (num_predict)
MAX_USER_CHARS = 4000    # one question; longer is refused (413)
MAX_HISTORY = 40         # messages the browser may send back
MAX_BODY_BYTES = 256_000

# One answer at a time per account: the GPU is shared and serial, a
# second request would only queue behind the first.
_in_flight: set[int] = set()


def enabled() -> bool:
    """True when an Ollama URL is configured."""
    return bool(OLLAMA_URL)


def try_acquire(user_id: int) -> bool:
    """Claim the account's single slot; False when one is running."""
    if user_id in _in_flight:
        return False
    _in_flight.add(user_id)
    return True


def release(user_id: int) -> None:
    """Free the account's slot (idempotent)."""
    _in_flight.discard(user_id)


# --- Texts -----------------------------------------------------------------

TEXTS = {
    "fr": {
        "disabled": "L'assistant n'est pas configuré sur ce serveur "
                    "(ASSISTANT_OLLAMA_URL est vide).",
        "busy": "Une réponse est déjà en cours : attends-la ou arrête-la.",
        "too_long": "Message trop long : raccourcis ta question.",
        "deadline": "Le modèle local a mis trop de temps à répondre "
                    "(il est peut-être occupé par une autre tâche). "
                    "Réessaie dans un moment.",
        "unreachable": "Impossible de joindre le modèle local. Réessaie "
                       "dans un moment.",
        "empty": "Le modèle n'a rien répondu. Reformule ta question ?",
        "final_round": "Dernier tour : plus d'outils. Réponds maintenant "
                       "avec ce que tu as déjà, recommandation comprise.",
        "thinking": "Je réfléchis…",
    },
    "en": {
        "disabled": "The assistant is not set up on this server "
                    "(ASSISTANT_OLLAMA_URL is empty).",
        "busy": "An answer is already running: wait for it or stop it.",
        "too_long": "Message too long: please shorten your question.",
        "deadline": "The local model took too long to answer (it may be "
                    "busy with another job). Try again in a moment.",
        "unreachable": "Could not reach the local model. Try again in a "
                       "moment.",
        "empty": "The model gave no answer. Could you rephrase?",
        "final_round": "Last round: no more tools. Answer now with what "
                       "you already have, recommendation included.",
        "thinking": "Thinking…",
    },
}


def text(language: str, key: str) -> str:
    """A UI/protocol string in the user's language (French fallback)."""
    return TEXTS.get(language, TEXTS["fr"])[key]


_WEEKDAYS = {
    "fr": ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
           "dimanche"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
           "Saturday", "Sunday"),
}

FR_SYSTEM_PROMPT = (
    "Tu es Smart Coach, le coach sportif, nutrition et sommeil de {name} : "
    "chaleureux, bienveillant, encourageant, avec un brin d'humour. Tu le "
    "tutoies. Il te pose ses questions dans l'application ; tu l'aides a "
    "comprendre ses donnees et a prendre ses decisions.\n"
    "DONNEES : tu ne sais rien sans tes outils. Avant de parler de ses "
    "chiffres, appelle les outils utiles (bilan = la vue d'ensemble du "
    "jour, deja triee ; aujourdhui = statut, seance du soir, budget ; "
    "puis sommeil, tendances, seances, entrainements (ce que la montre a "
    "enregistre : duree, calories, frequence cardiaque, zones), coeur "
    "(frequence cardiaque au repos, sur la journee, la nuit, pendant le "
    "sport, VFC), programme, repas, progres, ou donnees pour le reste : "
    "corps, activites, charge, recuperation, sante, mouvement, messages, "
    "trophees, meteo, reglages, synchro, calendrier ; et donnees_brutes "
    "pour lire n'importe quelle table telle quelle). Tu as acces a TOUTES "
    "ses donnees smart_coach, sauf les photos du Corps : ne dis jamais "
    "que tu n'y as pas acces, appelle l'outil ; si aucun outil de "
    "synthese ne repond, lis la table avec donnees_brutes. Chaque chiffre de ta reponse vient d'un resultat "
    "d'outil, tel quel : n'invente jamais un chiffre, un seuil, une "
    "quantite d'aliment. Une donnee absente, not_synced ou log_partial : "
    "dis que tu ne sais pas, n'en tire rien.\n"
    "DECIDER : s'il hesite (s'entrainer ou se reposer, changer la seance, "
    "ajuster ses repas, sortir d'un plateau), donne une recommandation "
    "claire en premier, puis les 2 ou 3 donnees qui la justifient, puis "
    "ce qui la ferait changer (avec les seuils fournis par les outils). "
    "Les decisions du code (statut, niveau, repos, recuperation, semaine "
    "allegee, regle de progression) passent avant ton avis : tu les "
    "expliques et tu proposes dans leur cadre, tu ne les recalcules pas.\n"
    "SANTE : tu n'es pas medecin. Jamais de diagnostic ('tu es malade' "
    "est interdit ; 'ton corps a l'air de lutter' est ok). Des signes "
    "de maladie (outil sante ou aujourdhui) = repos, pas de seance. De "
    "vrais symptomes (fievre, douleur ou gene dans la poitrine, "
    "essoufflement inhabituel, malaise, ou ca ne passe pas) : dis "
    "calmement de consulter un medecin.\n"
    "ACTIONS : noter_nuit, regler_niveau, declarer_malade, "
    "declarer_seance, regler_objectif, regler_parametre, regler_planning, "
    "seance_du_jour changent ses donnees : seulement "
    "s'il le demande ou dit oui clairement. Ensuite, confirme en une "
    "phrase ce qui a change, d'apres le resultat de l'outil (il peut "
    "annuler d'un geste). Ne dis JAMAIS qu'un reglage est change sans "
    "avoir appele l'outil et recu ok ; ce qu'aucun outil ne change, dis "
    "qu'il faut le faire dans Reglages.\n"
    "STYLE : court (2 a 6 phrases, ou une petite liste), en francais, "
    "Markdown simple (gras, listes). Pas de culpabilisation, aucune "
    "remarque sur son corps ou son poids, pas de jargon. "
    "Aujourd'hui : {weekday} {date}."
)

EN_SYSTEM_PROMPT = (
    "You are Smart Coach, {name}'s sports, nutrition and sleep coach: "
    "warm, kind, encouraging, with a touch of humour. They ask you "
    "questions in the app; you help them understand their data and make "
    "their decisions.\n"
    "DATA: you know nothing without your tools. Before talking about "
    "their numbers, call the useful tools (bilan = the day's overview, "
    "already sorted; aujourdhui = status, tonight's session, budget; "
    "then sommeil, tendances, seances, entrainements (what the watch "
    "recorded: duration, calories, heart rate, zones), coeur (heart rate "
    "at rest, over the day, at night, during exercise, HRV), programme, "
    "repas, progres, or donnees for the rest: body, activities, load, "
    "recovery, health, movement, messages, trophies, weather, settings, "
    "sync, calendar; and donnees_brutes to read any table as-is). You "
    "have access to ALL their smart_coach data except the Corps photos: "
    "never say you cannot access it, call the tool; when no summary tool "
    "answers, read the table with donnees_brutes. "
    "Every figure in your answer comes from a tool result, as-is: never "
    "invent a figure, a threshold or a food quantity. Missing data, "
    "not_synced or log_partial: say you do not know, draw nothing.\n"
    "DECIDE: when they hesitate (train or rest, swap the session, adjust "
    "meals, get past a plateau), give a clear recommendation first, then "
    "the 2 or 3 data points behind it, then what would change it (with "
    "the thresholds the tools give). The code's decisions (status, "
    "level, rest, recovery, lighter week, progression rule) come before "
    "your opinion: explain them and advise within them, never recompute "
    "them.\n"
    "HEALTH: you are not a doctor. Never a diagnosis ('you are sick' is "
    "forbidden; 'your body seems to be fighting something' is fine). "
    "Illness signs (health or aujourdhui tool) = rest, no session. Real "
    "symptoms (fever, chest pain or tightness, unusual breathlessness, "
    "feeling faint, or it does not go away): calmly say to see a doctor.\n"
    "ACTIONS: noter_nuit, regler_niveau, declarer_malade, "
    "declarer_seance, regler_objectif, regler_parametre, regler_planning, "
    "seance_du_jour change their data: only when they "
    "ask or clearly say yes. Then confirm in one sentence what changed, "
    "from the tool's result (they can undo it in one tap). NEVER say a "
    "setting changed without calling the tool and getting ok; what no "
    "tool changes, say it is done in Settings.\n"
    "STYLE: short (2 to 6 sentences, or a short list), in English, "
    "simple Markdown (bold, lists). No guilt, no remark about their body "
    "or weight, no jargon. Today: {weekday} {date}."
)


def system_prompt(language: str, name: str, date: str) -> str:
    """The chat's system prompt in the user's language."""
    language = language if language in ("fr", "en") else "fr"
    template = EN_SYSTEM_PROMPT if language == "en" else FR_SYSTEM_PROMPT
    weekday = _WEEKDAYS[language][dt.date.fromisoformat(date).weekday()]
    return template.format(name=name or "toi", date=date, weekday=weekday)


# --- Tool definitions (Ollama / OpenAI function format) ---------------------

DOMAINS = ("corps", "activites", "charge", "recuperation", "sante",
           "mouvement", "messages", "trophees", "meteo", "reglages",
           "synchro", "calendrier")
METRICS = ("poids", "fc_repos", "vfc", "pas", "calories", "proteines",
           "score_sommeil")
GOALS = {
    # key: (min, max) -- the Settings fields a chat may adjust.
    "step_goal": (1000, 40000),
    "floors_goal": (0, 100),
    "intensity_weekly_goal_min": (0, 1000),
    "protein_target_g_per_kg": (0.8, 3.0),
    "fat_target_g_per_kg": (0.5, 2.0),
    "hydration_target_ml_per_kg": (20, 60),
    "target_weight_kg": (30, 250),
    "weekly_weight_change_kg": (-1.0, 0.5),
    # A fixed daily calorie target; 0 = back to the computed one.
    "calorie_target_fixed_kcal": (1200, 5000),
}


def _fn(name: str, description: str, properties: dict | None = None,
        required: tuple = ()) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": list(required)},
    }}


# Every table of the account the chat may read row by row (donnees_brutes):
# name -> (date expression, order column). Left out on purpose: users,
# passkeys and settings (password hashes, keys, the link secret -- the
# safe settings are in donnees/reglages) and body_photos (the Corps
# photos and their analyses are never read by the assistant).
RAW_TABLES = {
    "poids": ("weight", "local_date", "time_utc"),
    "graisse": ("body_fat", "local_date", "time_utc"),
    "masse_maigre": ("lean_body_mass", "local_date", "time_utc"),
    "tour_de_taille": ("waist_measurements", "local_date", "local_date"),
    "fc_repos": ("resting_heart_rate", "local_date", "time_utc"),
    "fc_journee": ("heart_rate_daily", "local_date", "local_date"),
    "vfc": ("garmin_hrv", "local_date", "local_date"),
    "vo2max": ("garmin_vo2max", "local_date", "local_date"),
    "stress": ("garmin_stress", "local_date", "local_date"),
    "body_battery": ("garmin_body_battery", "local_date", "local_date"),
    "respiration": ("garmin_respiration", "local_date", "local_date"),
    "spo2": ("garmin_spo2", "local_date", "local_date"),
    "disponibilite": ("garmin_training_readiness", "local_date", "local_date"),
    "resume_garmin": ("garmin_daily_summary", "local_date", "local_date"),
    "minutes_intensives": ("garmin_intensity_minutes", "local_date", "local_date"),
    "cycle": ("garmin_menstrual_cycle", "local_date", "local_date"),
    "badges_garmin": ("garmin_badges", "earned_date", "earned_date"),
    "pas": ("steps", "local_date", "start_utc"),
    "distance": ("distance", "local_date", "start_utc"),
    "denivele": ("elevation_gained", "local_date", "start_utc"),
    "etages": ("floors_climbed", "local_date", "start_utc"),
    "calories_actives": ("active_calories", "local_date", "start_utc"),
    "calories_totales": ("total_calories_burned", "local_date", "start_utc"),
    "metabolisme_base": ("basal_metabolic_rate", "local_date", "time_utc"),
    "hydratation": ("hydration", "local_date", "start_utc"),
    "hydratation_garmin": ("garmin_hydration", "local_date", "local_date"),
    "repas": ("nutrition", "local_date", "start_utc"),
    "estimations_repas": ("meal_estimates", "local_date", "created_at"),
    "entrainements": ("exercise_sessions", "local_date", "start_utc"),
    "fc_entrainements": ("exercise_hr_samples", "date(epoch_utc)", "epoch_utc"),
    "parcours_gps": ("exercise_route_points", "date(epoch_utc)", "epoch_utc"),
    "nuits": ("sleep_sessions", "local_date", "start_utc"),
    "phases_sommeil": ("sleep_stages", "date(stage_start_utc)", "stage_start_utc"),
    "mesures_nuit": ("sleep_samples", "date(ts_utc)", "ts_utc"),
    "ressenti_nuits": ("sleep_feedback", "local_date", "local_date"),
    "decisions_du_coach": ("coach_log", "local_date", "id"),
    "notes_seances": ("session_feedback", "local_date", "local_date"),
    "seances_declarees": ("session_done", "local_date", "local_date"),
    "niveaux": ("levels", None, "session_type"),
    "semaines_allegees": ("deload_events", "date(triggered_at)", "triggered_at"),
    "charge": ("training_load", "local_date", "local_date"),
    "mes_mouvements": ("custom_moves", None, "id"),
    "mouvements_retires": ("removed_moves", None, "slot"),
    "quantites_fixees": ("slot_overrides", None, "slot"),
    "modifs_montre": ("watch_edits", "local_date", "id"),
    "seances_sur_la_montre": ("garmin_planned_workouts", "local_date", "local_date"),
    "messages": ("sent_messages", "local_date", "id"),
    "trophees": ("achievements", "date(unlocked_at)", "unlocked_at"),
    "xp": ("xp_ledger", "date", "id"),
    "synchros": ("ingest_runs", "date(ran_at)", "ran_at"),
    "couverture_export": ("hc_export_coverage", None, "table_name"),
}
# Columns that only link rows or carry machine state: dropped to save
# the context (never data the person would ask about).
RAW_HIDDEN = {"user_id", "uuid", "parent_uuid", "exercise_uuid", "signature",
              "shape", "diary_uuids", "workout_id", "values_json"}

READ_TOOLS = [
    _fn("bilan", "Today's decision-ready overview, the same one the "
        "morning message is written from: body signals, tonight's "
        "session and why, yesterday's review, what changed, help. Start "
        "here for any broad question."),
    _fn("aujourdhui", "Today's readiness (green/yellow/red) with every "
        "signal behind it and the thresholds, tonight's planned session "
        "with its values, illness signs, the calorie/macro budget and "
        "what is logged so far."),
    _fn("sommeil", "Recent nights: duration, watch score, awakenings, "
        "how the person said they slept, conclusions.",
        {"jours": {"type": "integer", "description": "1-14, default 7"}}),
    _fn("tendances", "Compact trend of one metric: min/max/average, "
        "first vs last week, direction.",
        {"metrique": {"type": "string", "enum": list(METRICS)},
         "jours": {"type": "integer", "description": "7-90, default 30"}},
        ("metrique",)),
    _fn("seances", "Recent sessions per day: planned, done, skipped or "
        "replaced, minutes, rating, level, and what the watch recorded "
        "(average/max heart rate, zones, calories).",
        {"jours": {"type": "integer", "description": "1-28, default 14"}}),
    _fn("entrainements", "The workouts the watch RECORDED (Garmin): "
        "date, start time, type, duration, calories, average and max "
        "heart rate, time in each heart-rate zone, effort felt, and the "
        "session that was planned. With jour, one workout in detail with "
        "its heart-rate curve. Use for any question about a workout, an "
        "activity or the heart rate during exercise.",
        {"jours": {"type": "integer", "description": "1-60, default 14"},
         "jour": {"type": "string",
                  "description": "optional: aujourdhui, hier or YYYY-MM-DD"}}),
    _fn("coeur", "Everything about the heart: resting heart rate (trend "
        "and personal baseline), heart-rate variability (HRV), heart "
        "rate over the day (min/average/max), during the night, during "
        "recent workouts, max heart rate and the zone limits in bpm. "
        "Use for any question about heart rate, pulse, cardio, HRV.",
        {"jours": {"type": "integer", "description": "7-90, default 14"}}),
    _fn("programme", "The week plan, the level per session type, what "
        "makes each go up, lighter weeks, the progression rules."),
    _fn("repas", "What was eaten that day, meal by meal, against the "
        "targets; for today, what is left and sized food ideas.",
        {"jour": {"type": "string",
                  "description": "aujourdhui, hier or YYYY-MM-DD"}}),
    _fn("progres", "Weight, body fat, calorie balance, protein, plateau "
        "and recalibration analysis, week review, waist."),
    _fn("donnees_brutes", "Raw rows of ANY table of the account, for "
        "what no other tool answers (newest first, at most 50). Names "
        "say what they hold: poids, fc_repos, fc_journee, "
        "fc_entrainements (bpm samples), entrainements, nuits, "
        "phases_sommeil, mesures_nuit, repas, pas, vo2max, stress, "
        "messages, decisions_du_coach...",
        {"table": {"type": "string", "enum": list(RAW_TABLES)},
         "du": {"type": "string", "description": "YYYY-MM-DD, default 7 days ago"},
         "au": {"type": "string", "description": "YYYY-MM-DD, default today"},
         "limite": {"type": "integer", "description": "1-50, default 30"}},
        ("table",)),
    _fn("donnees", "Any other data domain, summarised.",
        {"domaine": {"type": "string", "enum": list(DOMAINS)},
         "jours": {"type": "integer", "description": "window, default 7"}},
        ("domaine",)),
]
# Settings the chat may change (regler_parametre), each checked like the
# Settings form checks it. Never the addresses, topics, calendar or
# Drive names, keys or anything private.
PARAMS = {
    "kettlebell_kg": ("number", 2, 48),
    "session_cap_min": ("number", 10, 240),
    "kettlebell_circuits": ("choice", ("A", "AB", "B")),
    "message_style": ("choice", ("texte", "fiche")),
    "ask_sleep": ("bool",),
    "remind_mfp_sync": ("bool",),
    "desk_job": ("bool",),
    "city": ("text", 60),
    "tdee_method": ("choice", ("montre", "bmr_actif", "formule")),
    "activity_factor": ("number", 1.1, 1.9),
}
_DAY_NAMES = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
              "dimanche")

WRITE_TOOLS = [
    _fn("noter_nuit", "WRITE: record how last night felt (the Home "
        "page's buttons). Only when asked.",
        {"ressenti": {"type": "string", "enum": list(sleep_feedback.RATINGS)}},
        ("ressenti",)),
    _fn("regler_niveau", "WRITE: set tonight's session level (0-10), "
        "like the Home page's level field; also updates the calendar. "
        "Only on a training day, only when asked.",
        {"niveau": {"type": "integer"}}, ("niveau",)),
    _fn("declarer_malade", "WRITE: declare being ill for N days counting "
        "today (the coach then plans rest); 0 = better again. Only when "
        "asked.", {"jours": {"type": "integer", "description": "0-14"}},
        ("jours",)),
    _fn("declarer_seance", "WRITE: record what was actually done on a "
        "day (Sessions page 'J'ai fait'): a session type, other or "
        "nothing. Only when asked.",
        {"jour": {"type": "string",
                  "description": "aujourdhui, hier or YYYY-MM-DD"},
         "seance": {"type": "string", "enum": [
             *training.SESSION_LABEL_FR, done_sessions.OTHER,
             done_sessions.NOTHING]}},
        ("jour", "seance")),
    _fn("regler_parametre", "WRITE: change one setting: kettlebell "
        "weight in kg (levels go down for a heavier one), max session "
        "length in minutes, kettlebell circuits, morning message style, "
        "morning sleep question / evening MyFitnessPal reminder / desk "
        "break on or off (oui/non), weather city, what the calorie "
        "target starts from (tdee_method: montre, bmr_actif, formule = "
        "without the watch) and the daily activity factor for the "
        "formula. Only when asked.",
        {"parametre": {"type": "string", "enum": list(PARAMS)},
         "valeur": {"type": "string"}}, ("parametre", "valeur")),
    _fn("regler_planning", "WRITE: change one day of the week plan: its "
        "session (or repos), start time HH:MM, length in minutes. The "
        "watch and calendar follow. Only when asked.",
        {"jour": {"type": "string", "enum": list(_DAY_NAMES)},
         "seance": {"type": "string",
                    "enum": [*training.SESSION_LABEL_FR, "repos"]},
         "heure": {"type": "string", "description": "optional HH:MM"},
         "duree_min": {"type": "integer", "description": "optional"}},
        ("jour",)),
    _fn("seance_du_jour", "WRITE: today's session. envoyer = do the "
        "planned session anyway on a day the coach cancelled or lightened "
        "it (sent to the watch); revenir = back to the coach's call. "
        "pas_faite / sans_montre / demain = a planned session not "
        "recorded (demain only when tomorrow is a rest day). plus_lourd / "
        "plus_long = after a session too easy. Only when asked.",
        {"action": {"type": "string", "enum": [
            "envoyer", "revenir", "pas_faite", "sans_montre", "demain",
            "plus_lourd", "plus_long"]},
         "jour": {"type": "string",
                  "description": "for pas_faite/sans_montre: aujourdhui or hier"}},
        ("action",)),
    _fn("regler_objectif", "WRITE: change one goal from Settings. Only "
        "when asked. A daily calorie target ('mets-moi a 1900 kcal') is "
        "calorie_target_fixed_kcal (0 = computed again every day).",
        {"objectif": {"type": "string", "enum": list(GOALS)},
         "valeur": {"type": "number"}}, ("objectif", "valeur")),
]
TOOLS = READ_TOOLS + WRITE_TOOLS
WRITE_NAMES = {tool["function"]["name"] for tool in WRITE_TOOLS}

TOOL_LABELS = {
    "fr": {"bilan": "Bilan du jour", "aujourdhui": "Aujourd'hui",
           "sommeil": "Sommeil", "tendances": "Tendances",
           "seances": "Séances", "entrainements": "Entraînements",
           "coeur": "Cœur", "programme": "Programme", "repas": "Repas",
           "progres": "Progrès", "donnees": "Données",
           "donnees_brutes": "Données brutes",
           "noter_nuit": "Nuit notée", "regler_niveau": "Niveau du soir",
           "declarer_malade": "Maladie", "declarer_seance": "Séance notée",
           "regler_objectif": "Objectif", "regler_parametre": "Réglage",
           "regler_planning": "Planning", "seance_du_jour": "Séance du jour"},
    "en": {"bilan": "Today's overview", "aujourdhui": "Today",
           "sommeil": "Sleep", "tendances": "Trends",
           "seances": "Sessions", "entrainements": "Workouts",
           "coeur": "Heart", "programme": "Programme", "repas": "Meals",
           "progres": "Progress", "donnees": "Data",
           "donnees_brutes": "Raw data",
           "noter_nuit": "Night logged", "regler_niveau": "Tonight's level",
           "declarer_malade": "Illness", "declarer_seance": "Session logged",
           "regler_objectif": "Goal", "regler_parametre": "Setting",
           "regler_planning": "Plan", "seance_du_jour": "Today's session"},
}


class ToolError(ValueError):
    """A tool call the model got wrong: told back to it, not raised."""


# --- Small helpers ----------------------------------------------------------

def _r(value, digits: int = 1):
    if isinstance(value, float):
        rounded = round(value, digits)
        return int(rounded) if digits == 0 else rounded
    return value


def _int(args: dict, key: str, default: Optional[int], lo: int, hi: int,
         clamp: bool = True) -> int:
    """An integer argument; models send "7" as often as 7."""
    raw = args.get(key, default)
    if raw is None:
        raise ToolError(f"missing argument: {key}")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        raise ToolError(f"{key} must be an integer") from None
    if clamp:
        return max(lo, min(hi, value))
    if not lo <= value <= hi:
        raise ToolError(f"{key} must be between {lo} and {hi}")
    return value


def _choice(args: dict, key: str, allowed, default: Optional[str] = None) -> str:
    value = args.get(key, default)
    if value is None:
        raise ToolError(f"missing argument: {key}")
    value = str(value).strip().lower()
    if value not in allowed:
        raise ToolError(f"{key} must be one of {sorted(allowed)}")
    return value


def _day(args: dict, key: str, today: str, max_back: int) -> str:
    """``aujourdhui`` / ``hier`` / ISO date, within ``max_back`` days."""
    raw = str(args.get(key) or "aujourdhui").strip().lower()
    base = dt.date.fromisoformat(today)
    if raw in ("aujourdhui", "aujourd'hui", "today"):
        return today
    if raw in ("hier", "yesterday"):
        return (base - dt.timedelta(days=1)).isoformat()
    try:
        day = dt.date.fromisoformat(raw)
    except ValueError:
        raise ToolError(f"{key}: aujourdhui, hier or YYYY-MM-DD") from None
    if day > base or (base - day).days > max_back:
        raise ToolError(f"{key} must be within the last {max_back} days")
    return day.isoformat()


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def fit(obj: Any, cap: int = RESULT_CAP) -> str:
    """Serialise ``obj`` in at most ``cap`` chars, still valid JSON.

    Shrinks the biggest thing first -- the longest list loses its last
    item, the longest string is cut -- until it fits, so a long night
    list or a chatty message history degrades gracefully instead of
    being chopped mid-value.
    """
    out = _json(obj)
    if len(out) <= cap:
        return out
    obj = json.loads(out)  # a private, plain copy to shrink
    for _ in range(400):
        biggest, size = None, 0

        def walk(node, parent, key):
            nonlocal biggest, size
            if isinstance(node, (list, str)):
                length = len(_json(node))
                if length > size and (isinstance(node, str) and len(node) > 40
                                      or isinstance(node, list) and node):
                    biggest, size = (parent, key, node), length
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, node, k)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, node, i)

        walk(obj, None, None)
        if biggest is None:
            break
        parent, key, node = biggest
        if isinstance(node, list):
            node.pop()
        else:
            parent[key] = node[: max(40, len(node) // 2)] + "…"
        if isinstance(obj, dict):
            obj["tronque"] = True
        out = _json(obj)
        if len(out) <= cap:
            return out
    return out[: cap - 1] + "…"


def _series_summary(series: dict, unit: str, step: float, digits: int = 1) -> dict:
    """min/max/average, first vs last week and a direction for a
    ``{date: value}`` series; ``step`` is the change per week below
    which it reads as stable."""
    points = sorted((d, v) for d, v in series.items() if v is not None)
    if not points:
        return {"jours_avec_donnee": 0}
    values = [v for _, v in points]
    first = [v for d, v in points if d <= _plus(points[0][0], 6)]
    last = [v for d, v in points if d >= _plus(points[-1][0], -6)]
    avg = lambda xs: sum(xs) / len(xs)  # noqa: E731
    out = {
        "unite": unit, "jours_avec_donnee": len(points),
        "du": points[0][0], "au": points[-1][0],
        "min": _r(float(min(values)), digits), "max": _r(float(max(values)), digits),
        "moyenne": _r(avg(values), digits),
        "derniere": {"date": points[-1][0], "valeur": _r(float(values[-1]), digits)},
        "moyenne_premiere_semaine": _r(avg(first), digits),
        "moyenne_derniere_semaine": _r(avg(last), digits),
    }
    span_days = (dt.date.fromisoformat(points[-1][0])
                 - dt.date.fromisoformat(points[0][0])).days
    if len(points) >= 4 and span_days >= 10:
        change = avg(last) - avg(first)
        weeks = max(1.0, (span_days - 6) / 7)
        per_week = change / weeks
        out["ecart_par_semaine"] = _r(per_week, max(digits, 1))
        out["direction"] = (
            "stable" if abs(per_week) < step
            else "hausse" if per_week > 0 else "baisse"
        )
    return out


def _plus(date: str, days: int) -> str:
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


def _dates(today: str, days: int) -> list[str]:
    return [_plus(today, -k) for k in range(days - 1, -1, -1)]


def _pick(src: dict, keys) -> dict:
    return {k: _r(src[k]) for k in keys if src.get(k) is not None}


# --- Read tools ------------------------------------------------------------
# Each takes (conn, ctx, args) and returns a JSON-able dict. ctx carries
# user_id / username / today / language, all from the session -- never
# from the model's arguments.

def _plan_today(conn, ctx):
    """(entry, plan, template) for today, plan None before the run."""
    entry = day_actions.coach_entry(conn, ctx["user_id"], ctx["today"])
    template = missed_sessions.day_template(conn, ctx["user_id"], ctx["today"])
    plan = None
    if entry and (entry["session_type"] or entry["tier"]):
        plan = training.plan_from_log(conn, ctx["user_id"], ctx["today"],
                                      template, entry)
    return entry, plan, template


def tool_bilan(conn, ctx, args) -> dict:
    uid, today, language = ctx["user_id"], ctx["today"], ctx["language"]
    _entry, plan, template = _plan_today(conn, ctx)
    session = (
        training.session_payload(plan, template, language) if plan
        else {"type": "off_system", "tier": "train",
              "note": "le coach du matin n'a pas encore tourne aujourd'hui"
              if language == "fr" else "the morning run has not run yet today"}
    )
    payload = coach_payload.build_payload(
        conn, uid, today, language, session,
        training.illness_watch(conn, uid, today),
    )
    brief = coach_brief.build_brief(payload)
    brief.pop("language", None)
    # Yesterday's workout as the watch recorded it, heart rate included.
    yesterday = _workouts(conn, uid, _plus(today, -1), _plus(today, -1),
                          metrics.local_tz(conn, uid))
    if yesterday:
        brief["entrainement_hier_montre"] = [
            {k: w[k] for k in ("type", "duree_min", "fc_moyenne", "fc_max",
                               "temps_par_zone_pct") if k in w} for w in yesterday]
    return brief


def _thresholds() -> dict:
    """The status rules, from the constants the decision uses."""
    return {
        "sommeil_score": {"vert_des": training.SLEEP_VOTE_GREEN,
                          "rouge_sous": training.SLEEP_VOTE_RED},
        "recuperation_garmin": {"vert_des": training.READINESS_VOTE_GREEN,
                                "rouge_sous": training.READINESS_VOTE_RED},
        "fc_repos": f"rouge a +{training.RHR_SPIKE_RED_MIN} bpm sur ta base",
        "regle": "vert si rien n'est rouge et peu de signaux moyens "
                 "(batterie et stress comptent pour moitie) ; rouge sur une "
                 "FC de repos, une VFC ou un ressenti alarmant, ou deux "
                 "signaux rouges ; muscles du soir encore fatigues = le "
                 "niveau ne monte pas",
    }


def _session_values(values: dict | None) -> dict:
    """Tonight's numbers without the per-move metadata (cues, Garmin
    keys, muscle weights): each move by its name, with its count."""
    values = dict(values or {})
    variants = values.pop("variants", None) or {}
    out = {}
    for key, value in values.items():
        if isinstance(value, (dict, list)):
            continue
        name = (variants.get(key) or {}).get("name")
        out[name or key] = value
    return out


def tool_aujourdhui(conn, ctx, args) -> dict:
    uid, today = ctx["user_id"], ctx["today"]
    entry, plan, template = _plan_today(conn, ctx)
    wellness = metrics.daily_wellness(conn, uid, today)
    out: dict = {"date": today, "prevu_au_planning": template.get("title")}
    session_type = entry["session_type"] if entry else None
    if plan:
        out["seance_du_soir"] = {
            "type": session_type, "tier": plan["tier"],
            "niveau": plan["level"], "description": plan["description"],
            "valeurs": _session_values(plan["values"]),
            **({"pourquoi_tier": plan["tier_reasons"]} if plan["tier_reasons"] else {}),
            **({"pourquoi_niveau": plan["level_reason"]} if plan["level_reason"] else {}),
            "semaine_allegee": plan["in_deload"],
        }
        out["statut"] = entry["status"]
    else:
        out["coach_du_matin"] = "pas encore passe aujourd'hui"
    feedback = (training.recent_feedback(conn, uid, session_type)
                if session_type else [])
    out["signaux"] = [
        {"signal": s["label"], "vote": s["vote"], "detail": s["detail"]}
        for s in training.explain_status(
            wellness, training.rhr_baseline(conn, uid, today), feedback,
            training.muscle_votes(conn, uid, today, session_type),
        )
    ]
    out["seuils"] = _thresholds()
    illness = training.illness_watch(conn, uid, today)
    sick = training.sick_until(conn, uid)
    if illness.get("suspected") or sick:
        out["maladie"] = {**illness, "declare_jusqu_au": sick}
    felt = sleep_feedback.felt(conn, uid, today)
    if felt:
        out["nuit_ressentie"] = felt
    out["mesures"] = _pick(wellness, (
        "sleep_score", "hrv_last_night_avg", "hrv_status", "resting_hr",
        "training_readiness_score", "body_battery_highest",
        "stress_avg_yesterday", "steps_today", "step_goal",
    ))
    budget = progress.remaining_today(conn, uid, today)
    out["budget"] = {
        "objectifs": _pick(budget["targets"], (
            "calorie_target_kcal", "protein_target_g", "fat_target_g",
            "carb_target_g", "hydration_target_ml")),
        "note_jusqu_ici": _pick(budget["logged"], (
            "calories_kcal", "protein_g", "fat_g", "carbs_g", "hydration_ml")),
        "reste": budget["remaining"],
    }
    fatigue = muscles.muscle_fatigue(conn, uid, today,
                                     at=dt.datetime.now(dt.timezone.utc))
    tired = [
        {"muscle": muscles.MUSCLES.get(key, key), "etat": info["state"],
         **({"cause": info["cause"]} if info.get("cause") else {})}
        for key, info in sorted(fatigue["muscles"].items(),
                                key=lambda kv: -kv[1]["fatigue"])
        if info["state"] != "ready"
    ][:5]
    if tired:
        out["muscles_fatigues"] = tired
    return out


def tool_sommeil(conn, ctx, args) -> dict:
    days = _int(args, "jours", 7, 1, 14)
    report = sleep_analysis.period(conn, ctx["user_id"], ctx["today"], days)
    nights = [{
        "date": n["date"], "coucher": n["bed"], "lever": n["wake"],
        "sommeil_estime_min": n["sleep_min"], "montre_min": n["watch_sleep_min"],
        "score_montre": n["score"],
        "reveils": [w["at"] for w in n["wakes"]],
        **({"reveil_final": n["final_wake"]} if n["final_wake"] else {}),
        **({"ressenti": n["felt"]} if n["felt"] else {}),
    } for n in report["nights"]]
    out = {"jours": days, "nuits": nights}
    for key in ("sleep_min", "watch_sleep_min", "in_bed_min",
                "wakes_per_night", "short_nights", "debt_min", "bed_target"):
        if report.get(key) is not None:
            out[key] = report[key]
    if report.get("bed"):
        out["coucher_median"] = report["bed"]["median"]
    if report.get("conclusions"):
        out["conclusions"] = report["conclusions"]
    if not nights:
        out["note"] = "aucune nuit enregistree sur la periode"
    return out


def tool_tendances(conn, ctx, args) -> dict:
    uid, today = ctx["user_id"], ctx["today"]
    metric = _choice(args, "metrique", METRICS)
    days = _int(args, "jours", 30, 7, 90)
    dates = _dates(today, days)
    if metric == "poids":
        # One value per weigh-in day, averaged like the Progress page's
        # chart (the scale writes to Garmin and Health Connect both).
        series = {r["local_date"]: r["kg"] for r in conn.execute(
            "SELECT local_date, AVG(kg) AS kg FROM weight WHERE user_id = ? "
            "AND local_date BETWEEN ? AND ? GROUP BY local_date",
            (uid, dates[0], today))}
        out = _series_summary(series, "kg", 0.2)
        trend = progress.weight_trend(conn, uid, today)
        if trend:
            out["moyenne_14j_contre_precedente"] = trend
    elif metric == "fc_repos":
        out = _series_summary(
            {d: metrics.resting_hr_for_date(conn, uid, d) for d in dates},
            "bpm", 1, 0)
        base = training.rhr_baseline(conn, uid, today)
        if base:
            out["base_personnelle"] = round(base, 1)
    elif metric == "vfc":
        out = _series_summary(
            {d: metrics.garmin_wellness(conn, uid, d).get("hrv_last_night_avg")
             for d in dates}, "ms", 2, 0)
    elif metric == "pas":
        out = _series_summary(metrics.steps_for_range(conn, uid, today, days),
                              "pas", 500, 0)
        goal = db.get_setting(conn, uid, "step_goal")
        if goal:
            out["objectif"] = int(goal)
    elif metric == "score_sommeil":
        out = _series_summary(
            metrics.sleep_scores_for_range(conn, uid, today, days), "/100", 3, 0)
    else:
        key = "calories_kcal" if metric == "calories" else "protein_g"
        out = _series_summary(
            {d: progress.intake_for_date(conn, uid, d).get(key) for d in dates},
            "kcal" if metric == "calories" else "g",
            100 if metric == "calories" else 5, 0)
        targets = progress.macro_targets(conn, uid, today)
        target = targets.get("calorie_target_kcal" if metric == "calories"
                             else "protein_target_g")
        if target:
            out["objectif_du_jour"] = _r(target, 0)
        out["note"] = ("jours sans repas notes = absents, pas zero ; "
                       "aujourd'hui peut etre incomplet")
    return {"metrique": metric, "jours": days, **out}


def tool_seances(conn, ctx, args) -> dict:
    uid, today = ctx["user_id"], ctx["today"]
    days = _int(args, "jours", 14, 1, 28)
    levels = {r["date"]: r["level"]
              for r in training.rateable_sessions(conn, uid, today, days)}
    # The watch's recording of each day, heart rate included: asked
    # about "ma seance de tapis", the model reads this tool first, and
    # without it answered that the watch had sent no heart rate.
    recorded: dict = {}
    for workout in _workouts(conn, uid, _plus(today, -(days - 1)), today,
                             metrics.local_tz(conn, uid)):
        recorded.setdefault(workout["date"], []).append(
            {k: workout[k] for k in ("heure", "type", "duree_min", "kcal_actives",
                                     "fc_moyenne", "fc_max", "temps_par_zone_pct",
                                     "fc") if k in workout})
    rows = []
    for day in reversed(_dates(today, days)):
        # yesterday_training(d + 1) reviews day d: planned, done, minutes,
        # rating -- the same review the morning message gives.
        review = day_review.yesterday_training(conn, uid, _plus(day, 1))
        if not review["planned"] and not review["done"] and not review["minutes"]:
            continue
        tier = review["planned_tier"]
        if day == today and not review["done"]:
            verdict = "a venir"
        elif review["done"] == "rien" or (
                tier == training.TIER_TRAIN and review["planned"]
                and not review["done"] and not review["minutes"]):
            verdict = "manquee" if tier == training.TIER_TRAIN else "repos respecte"
        elif tier in ("rest", "recovery") and not review["done"]:
            verdict = "repos" if tier == "rest" else "recuperation"
        elif review["done"] and review["planned"] and review["done"] != review["planned"]:
            verdict = "remplacee"
        else:
            verdict = "faite"
        row = {"date": day, "prevu": review["planned"], "tier": tier,
               "fait": review["done"], "minutes": review["minutes"],
               "ressenti": review["rating"], "bilan": verdict}
        if day in levels:
            row["niveau"] = levels[day]
        if day in recorded:
            row["enregistre_par_la_montre"] = recorded[day]
        rows.append({k: v for k, v in row.items() if v is not None})
    counts: dict = {}
    for row in rows:
        counts[row["bilan"]] = counts.get(row["bilan"], 0) + 1
    return {"jours": days, "compte": counts, "par_jour": rows,
            "niveaux_actuels": {
                t: training.get_level(conn, uid, t)
                for t in sorted(training.scheduled_types(conn, uid))}}


def _zone_bounds(max_hr: Optional[int]) -> Optional[list]:
    """Z1..Z5 lower bounds in bpm for a max heart rate."""
    if not max_hr:
        return None
    return [{"zone": f"Z{i + 2}", "a_partir_de_bpm": round(max_hr * b)}
            for i, b in enumerate(metrics.HR_ZONE_BOUNDS_PCT)]


def _workouts(conn, uid: int, start: str, end: str, tz) -> list[dict]:
    """The recorded workouts between two local dates, newest first."""
    max_hr = metrics.estimated_max_hr(conn, uid)
    rows = conn.execute(
        "SELECT uuid, local_date, start_utc, end_utc, exercise_type, title, "
        "label_override, rpe FROM exercise_sessions WHERE user_id = ? AND "
        "local_date BETWEEN ? AND ? ORDER BY start_utc DESC LIMIT 40",
        (uid, start, end)).fetchall()
    out = []
    for row in rows:
        hr = conn.execute(
            "SELECT AVG(bpm) AS avg, MAX(bpm) AS max, MIN(bpm) AS min, "
            "COUNT(*) AS n FROM exercise_hr_samples WHERE user_id = ? AND "
            "exercise_uuid = ?", (uid, row["uuid"])).fetchone()
        kcal = conn.execute(
            "SELECT SUM(kcal) AS kcal FROM active_calories WHERE user_id = ? "
            "AND start_utc < ? AND end_utc > ?",
            (uid, row["end_utc"], row["start_utc"])).fetchone()["kcal"]
        begin = dt.datetime.fromisoformat(row["start_utc"])
        if begin.tzinfo is None:
            begin = begin.replace(tzinfo=dt.timezone.utc)
        item = {
            "uuid": row["uuid"], "date": row["local_date"],
            "heure": begin.astimezone(tz).strftime("%H:%M"),
            "type": (row["label_override"] or metrics.EXERCISE_TYPE_LABELS.get(
                row["exercise_type"], "autre")).replace("_", " "),
            "titre": row["title"] or None,
            "duree_min": metrics.session_duration_min(row),
            "kcal_actives": round(kcal) if kcal is not None else None,
            "effort_ressenti_rpe": row["rpe"],
        }
        if hr and hr["n"]:
            item.update(fc_moyenne=round(hr["avg"]), fc_max=hr["max"],
                        fc_min=hr["min"])
            zones = metrics.hr_zone_pct(conn, uid, row["uuid"], max_hr) if max_hr else []
            if zones:
                item["temps_par_zone_pct"] = {f"Z{i + 1}": z for i, z in enumerate(zones)}
        elif row["uuid"].startswith("garmin-"):
            item["fc"] = ("la montre n'a envoye aucune frequence cardiaque "
                          "pour cette activite")
        else:
            item["fc"] = ("activite venue de Health Connect : sa frequence "
                          "cardiaque est reprise de Garmin au prochain import")
        out.append({k: v for k, v in item.items() if v is not None})
    return out


def _hr_curve(conn, uid: int, uuid: str, step_min: int = 5) -> list[dict]:
    """A workout's heart rate averaged per ``step_min`` minutes."""
    rows = conn.execute(
        "SELECT epoch_utc, bpm FROM exercise_hr_samples WHERE user_id = ? AND "
        "exercise_uuid = ? ORDER BY epoch_utc", (uid, uuid)).fetchall()
    if not rows:
        return []
    first = dt.datetime.fromisoformat(rows[0]["epoch_utc"])
    buckets: dict[int, list[int]] = {}
    for row in rows:
        minute = (dt.datetime.fromisoformat(row["epoch_utc"]) - first).total_seconds() / 60
        buckets.setdefault(int(minute // step_min), []).append(row["bpm"])
    return [{"minute": k * step_min, "fc_moyenne": round(sum(v) / len(v)),
             "fc_max": max(v)} for k, v in sorted(buckets.items())][:24]


def tool_entrainements(conn, ctx, args) -> dict:
    uid, today = ctx["user_id"], ctx["today"]
    tz = metrics.local_tz(conn, uid)
    max_hr = metrics.estimated_max_hr(conn, uid)
    common = {"fc_max_estimee": max_hr, "zones": _zone_bounds(max_hr),
              "lecture_zones": "Z1 tres facile, Z2 endurance, Z3 tempo, "
                               "Z4 seuil, Z5 maximum (bornes : % de la FC max "
                               "estimee 220 - age)"}
    if args.get("jour"):
        day = _day(args, "jour", today, 90)
        workouts = _workouts(conn, uid, day, day, tz)
        for workout in workouts:
            workout["courbe_fc_par_5_min"] = _hr_curve(conn, uid, workout["uuid"])
        plan = conn.execute(
            "SELECT session_type, level, tier FROM coach_log WHERE user_id = ? "
            "AND local_date = ? ORDER BY id DESC LIMIT 1", (uid, day)).fetchone()
        rating = conn.execute(
            "SELECT rating FROM session_feedback WHERE user_id = ? AND "
            "local_date = ?", (uid, day)).fetchone()
        return {"jour": day, **common,
                "prevu": ({"seance": training.SESSION_LABEL_FR.get(
                    plan["session_type"], plan["session_type"]),
                    "niveau": plan["level"], "tier": plan["tier"]}
                    if plan and plan["session_type"] else None),
                "note_de_seance": rating["rating"] if rating else None,
                "entrainements": [{k: v for k, v in w.items() if k != "uuid"}
                                  for w in workouts]
                or "aucun entrainement enregistre ce jour-la"}
    days = _int(args, "jours", 14, 1, 60)
    workouts = _workouts(conn, uid, _plus(today, -(days - 1)), today, tz)
    with_hr = [w for w in workouts if "fc_moyenne" in w]
    return {
        "jours": days, **common,
        "total": {"nombre": len(workouts),
                  "minutes": sum(w["duree_min"] for w in workouts),
                  "kcal_actives": sum(w.get("kcal_actives", 0) for w in workouts),
                  "fc_moyenne_des_seances": (round(sum(w["fc_moyenne"] for w in with_hr)
                                                   / len(with_hr)) if with_hr else None)},
        "entrainements": [{k: v for k, v in w.items() if k != "uuid"}
                          for w in workouts],
    }


def tool_coeur(conn, ctx, args) -> dict:
    uid, today = ctx["user_id"], ctx["today"]
    days = _int(args, "jours", 14, 7, 90)
    dates = _dates(today, days)
    resting = _series_summary(
        {d: metrics.resting_hr_for_date(conn, uid, d) for d in dates}, "bpm", 1, 0)
    base = training.rhr_baseline(conn, uid, today)
    hrv = conn.execute(
        "SELECT local_date, last_night_avg, weekly_avg, status FROM garmin_hrv "
        "WHERE user_id = ? AND local_date BETWEEN ? AND ? ORDER BY local_date "
        "DESC", (uid, dates[0], today)).fetchall()
    day_rows = conn.execute(
        "SELECT local_date, min_bpm, avg_bpm, max_bpm FROM heart_rate_daily "
        "WHERE user_id = ? AND local_date BETWEEN ? AND ? ORDER BY local_date "
        "DESC LIMIT 7", (uid, dates[0], today)).fetchall()
    summary = metrics.garmin_daily_summary(conn, uid, _plus(today, -1))
    nights = []
    for k in range(3):
        night = sleep_analysis.night(conn, uid, _plus(today, -k))
        if night and night.get("has_hr"):
            nights.append({"nuit_finie_le": _plus(today, -k),
                           "fc_de_base": night.get("hr_base"),
                           "fc_min": night.get("hr_min"),
                           "reveils_avec_pic_fc": [
                               {"a": w["at"], "pic_bpm": w.get("peak_bpm")}
                               for w in night.get("wakes") or []
                               if w.get("peak_bpm")][:5]})
    tz = metrics.local_tz(conn, uid)
    workouts = [w for w in _workouts(conn, uid, dates[0], today, tz)
                if "fc_moyenne" in w][:6]
    max_hr = metrics.estimated_max_hr(conn, uid)
    return {
        "jours": days,
        "fc_repos": {**resting, **({"base_personnelle": round(base, 1)} if base else {}),
                     "lecture": "plus basse = mieux recupere ; 5 bpm ou plus "
                                "au-dessus de la base = fatigue ou maladie possible"},
        "vfc": [{"date": r["local_date"], "nuit_ms": _r(r["last_night_avg"], 0),
                 "moyenne_7j_ms": _r(r["weekly_avg"], 0), "statut": r["status"]}
                for r in hrv[:7]] or "pas de VFC enregistree",
        "journee": ([{"date": r["local_date"], "min": r["min_bpm"],
                      "moyenne": _r(r["avg_bpm"], 0), "max": r["max_bpm"]}
                     for r in day_rows]
                    or ({"hier_min": summary.get("min_hr"),
                         "hier_max": summary.get("max_hr")}
                        if summary.get("max_hr") else "pas de donnee")),
        "nuits": nights or "pas de frequence cardiaque de nuit enregistree",
        "pendant_les_entrainements": [
            {k: w[k] for k in ("date", "type", "duree_min", "fc_moyenne",
                               "fc_max", "temps_par_zone_pct") if k in w}
            for w in workouts] or "aucun entrainement avec frequence cardiaque",
        "fc_max_estimee": max_hr, "zones": _zone_bounds(max_hr),
        "rappel": "pas un avis medical",
    }


def tool_programme(conn, ctx, args) -> dict:
    uid, today = ctx["user_id"], ctx["today"]
    names = _WEEKDAYS["fr"]
    schedule = training.schedule_for_user(conn, uid)
    week = []
    for weekday in range(7):
        item = schedule[weekday]
        week.append({
            "jour": names[weekday], "titre": item.get("title"),
            "type": item.get("session_type") or "hors systeme",
            "heure": item.get("start"), "duree_min": item.get("duration_min"),
            **({"aujourdhui": True}
               if weekday == dt.date.fromisoformat(today).weekday() else {}),
        })
    types = {}
    for session_type in sorted(training.scheduled_types(conn, uid)):
        level = training.get_level(conn, uid, session_type)
        until = training.get_deload_until(conn, uid, session_type)
        types[session_type] = {
            "nom": training.SESSION_LABEL_FR.get(session_type),
            "niveau": level, "sur": training.LEVEL_MAX,
            **({"pour_monter": hint} if (hint := training.progress_hint(
                conn, uid, session_type, today, level)) else {}),
            **({"semaine_allegee_jusqu_au": until}
               if until and until >= today else {}),
        }
    out = {
        "semaine": week, "seances": types,
        "regles": (
            f"Un niveau monte d'un cran apres {training.STEADY_SESSIONS} "
            "seances faites a ce niveau sans 'trop dure', ou des une "
            "seance notee 'trop facile', un jour non rouge ; jour jaune = "
            "meme niveau ; jour rouge = seance allegee ou marche de "
            "recuperation ; 3 jours rouges de suite ou une fatigue "
            f"accumulee (TSB sous {training.TSB_DELOAD_THRESHOLD:g}) = "
            "semaine allegee ; maladie = repos."),
        "duree_max_min": training.session_cap_min(conn, uid),
    }
    bell = training.heavier_bell_hint(conn, uid)
    if bell:
        out["kettlebell_trop_legere"] = bell
    return out


def tool_repas(conn, ctx, args) -> dict:
    uid, today, language = ctx["user_id"], ctx["today"], ctx["language"]
    day = _day(args, "jour", today, 60)
    intake = progress.intake_for_date(conn, uid, day)
    summary = meals.day_summary(conn, uid, day)
    out = {"date": day,
           "total": _pick(intake, ("calories_kcal", "protein_g", "carbs_g",
                                   "fat_g", "hydration_ml", "entries")),
           "par_repas": summary["meals"]}
    if summary["estimates"]:
        out["photos_estimees"] = summary["estimates"]
    if "calories_kcal" not in intake:
        out["note"] = "rien de note ce jour-la (ou pas encore synchronise)"
    targets = progress.macro_targets(conn, uid, day)
    out["objectifs"] = _pick(targets, ("calorie_target_kcal", "protein_target_g",
                                       "fat_target_g", "carb_target_g"))
    if day == today:
        remaining = progress.remaining_today(conn, uid, today)["remaining"]
        out["reste_aujourdhui"] = remaining
        gap = remaining.get("protein_g")
        if gap and gap > 4:
            # Sized by code (aids.food_help), never by the model.
            out["idees_pour_finir_les_proteines"] = aids.food_help(
                gap, today, language, office=False)
        over = remaining.get("calories_kcal")
        if over is not None and over < 0:
            out["si_calories_depassees"] = aids.lighter_dinner(-over, language)
    return out


def tool_progres(conn, ctx, args) -> dict:
    bundle = progress.weekly_progress(conn, ctx["user_id"], ctx["today"])
    keep = ("weight_trend_14d", "weight_progression", "body_fat_trend_28d",
            "lean_mass_trend_28d", "calorie_balance_7d", "protein_7d",
            "plateau", "recalibration", "observed_tdee", "week_review",
            "waist")
    out = {}
    for key in keep:
        value = bundle.get(key)
        if value:
            if isinstance(value, dict):
                value = {k: v for k, v in value.items()
                         if not isinstance(v, list) or len(v) <= 8}
            out[key] = value
    # Protein per DAY over the last 7 days (progress.protein_trend
    # averages per diary entry, so a day logged meal by meal reads as a
    # third of what was eaten).
    days = [progress.intake_for_date(conn, ctx["user_id"], d).get("protein_g")
            for d in _dates(_plus(ctx["today"], -1), 7)]
    logged = [p for p in days if p is not None]
    out.pop("protein_7d", None)
    if logged:
        target = progress.macro_targets(conn, ctx["user_id"], ctx["today"]).get(
            "protein_target_g")
        out["proteines_7j"] = {"moyenne_par_jour_g": round(sum(logged) / len(logged)),
                               "jours_notes": len(logged),
                               **({"objectif_g": round(target)} if target else {})}
    if not out:
        out["note"] = "pas encore assez de mesures pour une tendance"
    return out


SETTINGS_SHOWN = (
    "language", "timezone", "step_goal", "floors_goal",
    "intensity_weekly_goal_min", "protein_target_g_per_kg",
    "fat_target_g_per_kg", "hydration_target_ml_per_kg", "target_weight_kg",
    "weekly_weight_change_kg", "height_cm", "age_years", "sex",
    "calorie_target_fixed_kcal", "tdee_method", "activity_factor",
    "kettlebell_kg", "kettlebell_circuits", "desk_job", "ask_sleep",
    "message_style", "city", "trust_device_tdee", "bmr_manual_kcal",
)


def _domain(conn, ctx, domain: str, days: int) -> dict:
    uid, today, language = ctx["user_id"], ctx["today"], ctx["language"]
    if domain == "corps":
        context = body.build_context(conn, uid, today)
        context.pop("available_exercises", None)
        return {**context,
                "graisse_28j": progress.body_fat_trend(conn, uid, today),
                "masse_maigre_28j": progress.lean_mass_trend(conn, uid, today),
                "tour_de_taille": waist.trend(conn, uid, today),
                "tours_de_taille_recents": waist.history(conn, uid, 6),
                "note": "les photos du Corps ne sont jamais lues par "
                        "l'assistant"}
    if domain == "activites":
        days = max(1, min(28, days))
        activities = []
        for end in range(0, days, 7):  # history_snapshot covers 7 days
            snap = metrics.history_snapshot(conn, uid, _plus(today, -end))
            activities = snap["activities_last_7_days"] + activities
        start = _plus(today, -(days - 1))
        return {"jours": days,
                "activites": [a for a in activities if a["date"] >= start][::-1],
                "totaux": metrics.period_stats(conn, uid, today, days)}
    if domain == "charge":
        history = training_load.training_load_history(conn, uid, max(7, days))
        latest = training_load.latest_training_load(conn, uid)
        return {"derniere": latest,
                "il_y_a_n_jours": history[0] if history else None,
                "seuil_semaine_allegee_tsb": training.TSB_DELOAD_THRESHOLD,
                "lecture": "ctl = forme (42 j), atl = fatigue (7 j), "
                           "tsb = ctl - atl (negatif = fatigue accumulee)"}
    if domain == "recuperation":
        fatigue = muscles.muscle_fatigue(conn, uid, today,
                                         at=dt.datetime.now(dt.timezone.utc))
        return {"muscles": [
            {"muscle": muscles.MUSCLES.get(k, k), "etat": v["state"],
             "fatigue": _r(v["fatigue"], 2),
             "volume_7j": _r(v.get("week_volume"), 1),
             **({"cause": v["cause"]} if v.get("cause") else {})}
            for k, v in sorted(fatigue["muscles"].items(),
                               key=lambda kv: -kv[1]["fatigue"])][:10],
            "lecture": "ready = pret ; les autres etats = encore fatigue"}
    if domain == "sante":
        wellness = metrics.daily_wellness(conn, uid, today)
        return {
            "signes_de_maladie": training.illness_watch(conn, uid, today),
            "declare_malade_jusqu_au": training.sick_until(conn, uid),
            "mesures": _pick(wellness, (
                "resting_hr", "hrv_last_night_avg", "hrv_status",
                "spo2_average", "spo2_lowest", "respiration_avg_sleep",
                "stress_avg_level", "body_battery_highest",
                "body_battery_lowest", "menstrual_cycle_phase")),
            "consulter_si": (aids.CARE_DOCTOR_EN if language == "en"
                             else aids.CARE_DOCTOR_FR),
            "rappel": "pas un avis medical",
        }
    if domain == "mouvement":
        return {"hier": metrics.movement_summary(conn, uid, today),
                "semaine": metrics.period_stats(conn, uid, today, 7),
                "mois": metrics.period_stats(conn, uid, today, 30)}
    if domain == "messages":
        days = max(1, min(14, days))
        return {"jours": days, "messages": [
            {"date": d["date"], "heure": m["time"], "type": m["label"],
             "texte": (m["text"] or "")[:400]}
            for d in sent_log.history(conn, uid, days, today)
            for m in d["messages"]]}
    if domain == "trophees":
        import achievements

        recent = conn.execute(
            "SELECT key, unlocked_at FROM achievements WHERE user_id = ? "
            "ORDER BY unlocked_at DESC LIMIT 8", (uid,)).fetchall()
        name = "name_en" if language == "en" else "name_fr"
        desc = "desc_en" if language == "en" else "desc_fr"
        return {"score": achievements.score(conn, uid),
                "niveau_joueur": achievements.player_level(conn, uid),
                "derniers": [
                    {"nom": achievements.ACHIEVEMENTS[r["key"]][name],
                     "pour": achievements.ACHIEVEMENTS[r["key"]][desc],
                     "le": r["unlocked_at"]}
                    for r in recent if r["key"] in achievements.ACHIEVEMENTS]}
    if domain == "meteo":
        city = db.get_setting(conn, uid, "city")
        if not city:
            return {"note": "aucune ville reglee (Reglages)"}
        import weather

        try:
            forecast = weather.today_weather(
                city, tz=metrics.local_tz(conn, uid).key)
        except Exception as error:  # noqa: BLE001 -- told to the model
            raise ToolError(f"weather unavailable: {error}") from None
        return {"ville": city, "aujourdhui": forecast}
    if domain == "reglages":
        settings = {k: v for k in SETTINGS_SHOWN
                    if (v := db.get_setting(conn, uid, k)) not in (None, "")}
        return {"reglages": settings,
                "objectifs_du_jour": _pick(
                    progress.macro_targets(conn, uid, today),
                    ("calorie_target_kcal", "protein_target_g",
                     "fat_target_g", "carb_target_g", "hydration_target_ml")),
                "duree_max_seance_min": training.session_cap_min(conn, uid)}
    if domain == "synchro":
        tz = metrics.local_tz(conn, uid)
        rows = conn.execute(
            "SELECT "
            "  MAX(CASE WHEN table_name LIKE 'garmin:%' THEN ran_at END) AS garmin, "
            "  MAX(CASE WHEN table_name NOT LIKE 'garmin:%' THEN ran_at END) AS phone "
            "FROM ingest_runs WHERE user_id = ?", (uid,)).fetchone()

        def when(value):
            if not value:
                return None
            moment = dt.datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=dt.timezone.utc)
            return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        return {"garmin": when(rows["garmin"]), "telephone": when(rows["phone"]),
                "note": "au-dela de 30 h, une synchro a ete manquee"}
    if domain == "calendrier":
        days = max(1, min(14, days))
        schedule = training.schedule_for_user(conn, uid)
        upcoming = []
        for k in range(days):
            day = _plus(today, k)
            item = schedule[dt.date.fromisoformat(day).weekday()]
            upcoming.append({"date": day, "titre": item.get("title"),
                             "heure": item.get("start"),
                             "duree_min": item.get("duration_min")})
        on_watch = [dict(r) for r in conn.execute(
            "SELECT local_date, kind FROM garmin_planned_workouts WHERE "
            "user_id = ? AND local_date >= ? ORDER BY local_date LIMIT 7",
            (uid, today))]
        return {"a_venir": upcoming, "sur_la_montre": on_watch,
                "note": "l'agenda Google lui-meme n'est pas lu par "
                        "l'assistant ; le coach du matin y deplace la "
                        "seance en cas de conflit"}
    raise ToolError(f"domaine must be one of {list(DOMAINS)}")


def tool_donnees_brutes(conn, ctx, args) -> dict:
    uid, today = ctx["user_id"], ctx["today"]
    name = _choice(args, "table", RAW_TABLES)
    table, day_expr, order = RAW_TABLES[name]
    limit = _int(args, "limite", 30, 1, 50)
    start = _day({"d": args.get("du") or _plus(today, -6)}, "d", today, 3650)
    end = _day({"d": args.get("au") or today}, "d", today, 3650)
    if start > end:
        start, end = end, start
    columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")
               if r[1] not in RAW_HIDDEN]
    where, params = "user_id = ?", [uid]
    if day_expr:
        where += f" AND {day_expr} BETWEEN ? AND ?"
        params += [start, end]
    total = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}",
                         params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {where} "
        f"ORDER BY {order} DESC LIMIT ?", (*params, limit)).fetchall()

    def cell(value):
        if isinstance(value, str) and len(value) > 300:
            return value[:300] + "…"
        return _r(value) if isinstance(value, float) else value
    return {"table": name, **({"du": start, "au": end} if day_expr else {}),
            "lignes_dans_la_periode": total, "colonnes": columns,
            "lignes": [[cell(v) for v in row] for row in rows],
            **({"note": f"{total - len(rows)} lignes plus anciennes non "
                        "montrees : resserre du/au ou utilise un outil de "
                        "synthese"} if total > len(rows) else {})}


# What a model asks for in other words: sent to the right tool instead
# of an error it may give up on.
DOMAIN_ALIASES = {
    "coeur": "coeur", "cardio": "coeur", "frequence_cardiaque": "coeur",
    "fc": "coeur", "heart": "coeur", "vfc": "coeur", "hrv": "coeur",
    "entrainements": "entrainements", "entrainement": "entrainements",
    "workouts": "entrainements", "seances": "entrainements",
    "activities": "activites", "activite": "activites", "sport": "activites",
    "sommeil": "sommeil", "sleep": "sommeil", "repas": "repas",
    "nutrition": "repas", "poids": "corps", "body": "corps",
}


def tool_donnees(conn, ctx, args) -> dict:
    raw = str(args.get("domaine") or "").strip().lower().replace(" ", "_")
    target = DOMAIN_ALIASES.get(raw, raw)
    if target in READERS and target != "donnees":
        return {"domaine": raw, **READERS[target](conn, ctx, {
            k: v for k, v in args.items() if k != "domaine"})}
    domain = _choice({"domaine": target}, "domaine", DOMAINS)
    days = _int(args, "jours", 7, 1, 90)
    return {"domaine": domain, **_domain(conn, ctx, domain, days)}


# --- Write tools ------------------------------------------------------------
# Each returns (result, undo). ``undo`` is the same tool with the
# previous value, or None when there is nothing to put back.

def write_noter_nuit(conn, ctx, args, undo=False):
    uid, today = ctx["user_id"], ctx["today"]
    rating = _choice(args, "ressenti", sleep_feedback.RATINGS)
    previous = sleep_feedback.felt(conn, uid, today)
    sleep_feedback.record(conn, uid, today, rating)
    return ({"ok": True, "nuit_du": today, "ressenti": rating,
             "label": sleep_feedback.LABEL_FR[rating], "avant": previous},
            {"tool": "noter_nuit", "args": {"ressenti": previous}}
            if previous and previous != rating else None)


def write_regler_niveau(conn, ctx, args, undo=False):
    level = _int(args, "niveau", None, training.LEVEL_MIN, training.LEVEL_MAX,
                 clamp=False)
    done = day_actions.set_today_level(conn, ctx["user_id"], ctx["username"],
                                       ctx["today"], level)
    if done is None:
        raise ToolError("no editable level today (rest or recovery day, or "
                        "the morning run has not run yet)")
    return ({"ok": True, "seance": done["session_type"],
             "niveau": done["level"], "avant": done["previous"],
             **({"agenda": done["note"]} if done["note"] else {})},
            {"tool": "regler_niveau", "args": {"niveau": done["previous"]}}
            if done["previous"] is not None and done["previous"] != done["level"]
            else None)


def write_declarer_malade(conn, ctx, args, undo=False):
    uid, today = ctx["user_id"], ctx["today"]
    days = _int(args, "jours", None, 0, 14, clamp=False)
    previous = training.sick_until(conn, uid)
    previous_days = (
        (dt.date.fromisoformat(previous) - dt.date.fromisoformat(today)).days + 1
        if previous and previous >= today else 0
    )
    until = training.set_sick(conn, uid, today, days)
    return ({"ok": True, "malade_jusqu_au": until,
             "note": "le coach planifie du repos a partir de demain matin"
             if until else "plus declare malade"},
            {"tool": "declarer_malade", "args": {"jours": previous_days}}
            if previous_days != days else None)


def write_declarer_seance(conn, ctx, args, undo=False):
    uid, today = ctx["user_id"], ctx["today"]
    day = _day(args, "jour", today, done_sessions.DECLARE_DAYS)
    allowed = {*training.SESSION_LABEL_FR, done_sessions.OTHER,
               done_sessions.NOTHING}
    if undo:
        allowed.add("planned")  # back to the plan: the page's own choice
    choice = _choice(args, "seance", allowed)
    known = done_sessions.done_on(conn, uid, day)
    previous = (known["session_type"]
                if known and known.get("source") == "declared" else "planned")
    try:
        day_actions.declare_done(conn, uid, day, choice, today)
    except ValueError as error:
        raise ToolError(str(error)) from None
    label = training.SESSION_LABEL_FR.get(choice) or {
        done_sessions.OTHER: "une autre activite",
        done_sessions.NOTHING: "rien", "planned": "ce qui etait prevu"}[choice]
    return ({"ok": True, "jour": day, "fait": label},
            {"tool": "declarer_seance", "args": {"jour": day, "seance": previous}}
            if previous != choice else None)


def write_regler_objectif(conn, ctx, args, undo=False):
    uid = ctx["user_id"]
    key = _choice(args, "objectif", GOALS)
    previous = db.get_setting(conn, uid, key)
    raw = args.get("valeur")
    if undo and raw in ("", None):
        value = ""  # the goal was unset before: unset it again
    else:
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ToolError("valeur must be a number") from None
        lo, hi = GOALS[key]
        if key == "calorie_target_fixed_kcal" and number == 0:
            number = None  # back to the target computed every day
        elif not lo <= number <= hi:
            raise ToolError(f"{key} must be between {lo} and {hi}")
        value = "" if number is None else f"{number:g}"
    # Same storage as the Settings form (db.set_setting, stripped text).
    db.set_setting(conn, uid, key, value)
    return ({"ok": True, "objectif": key, "valeur": value or None,
             "avant": previous or None},
            {"tool": "regler_objectif",
             "args": {"objectif": key, "valeur": previous or ""}}
            if (previous or "") != value else None)


def write_regler_parametre(conn, ctx, args, undo=False):
    uid = ctx["user_id"]
    key = _choice(args, "parametre", PARAMS)
    spec = PARAMS[key]
    previous = db.get_setting(conn, uid, key)
    raw = str(args.get("valeur", "")).strip()
    if spec[0] == "number":
        try:
            number = float(raw.replace(",", "."))
        except ValueError:
            raise ToolError("valeur must be a number") from None
        if not spec[1] <= number <= spec[2]:
            raise ToolError(f"{key} must be between {spec[1]} and {spec[2]}")
        value = f"{number:g}"
    elif spec[0] == "choice":
        value = next((c for c in spec[1] if c.lower() == raw.lower()), None)
        if value is None:
            raise ToolError(f"{key} must be one of {list(spec[1])}")
    elif spec[0] == "bool":
        if raw.lower() in ("1", "oui", "on", "true", "yes"):
            value = "1"
        elif raw.lower() in ("0", "non", "off", "false", "no"):
            value = "0"
        else:
            raise ToolError("valeur: oui or non")
    else:
        if len(raw) > spec[1]:
            raise ToolError(f"{key}: at most {spec[1]} characters")
        value = raw
    levels_before = {t: training.get_level(conn, uid, t)
                     for t in training.LOADED_TYPES}
    db.set_setting(conn, uid, key, value)
    conn.commit()
    data = {"ok": True, "parametre": key, "valeur": value, "avant": previous or None}
    if key == "kettlebell_kg":
        if undo and isinstance(args.get("niveaux"), dict):
            # Undoing a heavier bell: the levels it lowered come back.
            for session_type, level in args["niveaux"].items():
                if session_type in training.LOADED_TYPES:
                    training.set_level(conn, uid, session_type, int(level))
        else:
            old_kg = float(previous) if previous else None
            lowered = training.lower_levels_for_heavier_bell(
                conn, uid, old_kg, float(value))
            if lowered:
                data["niveaux_ajustes"] = {t: f"{a} -> {b}"
                                           for t, (a, b) in lowered.items()}
    undo_args = {"parametre": key, "valeur": previous or ""}
    if key == "kettlebell_kg":
        undo_args["niveaux"] = levels_before
    return data, ({"tool": "regler_parametre", "args": undo_args}
                  if (previous or "") != value else None)


def write_regler_planning(conn, ctx, args, undo=False):
    uid = ctx["user_id"]
    weekday = _DAY_NAMES.index(_choice(args, "jour", _DAY_NAMES))
    schedule = {str(k): dict(v) for k, v in
                training.schedule_for_user(conn, uid).items()}
    before = dict(schedule[str(weekday)])
    if undo and isinstance(args.get("entree"), dict):
        entry = dict(args["entree"])
    else:
        entry = dict(before)
        if args.get("seance"):
            seance = _choice(args, "seance", {*training.SESSION_LABEL_FR, "repos"})
            entry["session_type"] = None if seance == "repos" else seance
            if seance != "repos":
                entry["title"] = training.SESSION_LABEL_FR[seance]
        if args.get("heure"):
            start = str(args["heure"]).strip().replace("h", ":")
            if len(start) == 4 and start[1] == ":":
                start = "0" + start
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", start):
                raise ToolError("heure: HH:MM")
            entry["start"] = start
        if args.get("duree_min") not in (None, ""):
            entry["duration_min"] = _int(args, "duree_min", None, 5, 240, clamp=False)
    schedule[str(weekday)] = entry
    db.set_setting(conn, uid, "schedule", json.dumps(schedule))
    conn.commit()
    import run_coach

    data = {"ok": True, "jour": _DAY_NAMES[weekday],
            "seance": training.SESSION_LABEL_FR.get(entry.get("session_type"), "repos"),
            "heure": entry.get("start"), "duree_min": entry.get("duration_min")}
    if run_coach.planned_change(conn, uid):
        data["note"] = ("la seance d'aujourd'hui a change : le bouton "
                        "Replanifier de l'accueil refait la journee (montre, "
                        "agenda, message)")
    else:
        data["note"] = "la montre et l'agenda suivront au prochain passage du coach"
    return data, ({"tool": "regler_planning",
                   "args": {"jour": _DAY_NAMES[weekday], "entree": before}}
                  if before != entry else None)


def write_seance_du_jour(conn, ctx, args, undo=False):
    import missed_sessions
    import progression_choice

    uid, today = ctx["user_id"], ctx["today"]
    action = _choice(args, "action", ("envoyer", "revenir", "pas_faite",
                                      "sans_montre", "demain", "plus_lourd",
                                      "plus_long", "annuler_reponse"))
    if action in ("envoyer", "revenir"):
        if action == "envoyer":
            offer = day_actions.send_anyway(conn, uid, today)
            if offer is None:
                raise ToolError("nothing to send anyway today: the coach did "
                                "not cancel or lighten today's session")
            data = {"ok": True, "envoye": f"{offer['label']} niveau {offer['level']}"}
        else:
            day_actions.cancel_anyway(conn, uid, today)
            data = {"ok": True, "retour": "choix du coach"}
        try:
            from ingest import garmin_api

            data["montre"] = garmin_api.refresh_today(
                conn, uid, garmin_api.get_client(ctx["username"]), today)
        except Exception as error:  # noqa: BLE001 -- told, never lost
            data["montre"] = f"envoi impossible : {error}"[:200]
        return data, {"tool": "seance_du_jour", "args": {
            "action": "revenir" if action == "envoyer" else "envoyer"}}
    if action in ("plus_lourd", "plus_long"):
        offer = progression_choice.offer(conn, uid, today)
        if offer is None:
            raise ToolError("no choice pending: it comes after a loaded "
                            "session rated too easy")
        note = progression_choice.choose(
            conn, uid, today, offer["date"], offer["type"],
            "heavier" if action == "plus_lourd" else "longer")
        if not note:
            raise ToolError("that choice is not possible now")
        return {"ok": True, "fait": note}, None
    day = _day(args, "jour", today, 2)
    if action == "annuler_reponse":
        done_sessions.clear(conn, uid, day)
        conn.execute("DELETE FROM postponed_sessions WHERE user_id = ? AND "
                     "from_date = ?", (uid, day))
        conn.commit()
        return {"ok": True, "annule": day}, None
    choice = {"pas_faite": "none", "sans_montre": "nowatch",
              "demain": "tomorrow"}[action]
    try:
        note = missed_sessions.answer(conn, uid, day, choice, today=today)
    except ValueError as error:
        raise ToolError(str(error)) from None
    if not note:
        raise ToolError("not possible: no training session planned that day, "
                        "or tomorrow is not a free rest day")
    return {"ok": True, "fait": note}, {"tool": "seance_du_jour", "args": {
        "action": "annuler_reponse", "jour": day}}


READERS: dict[str, Callable] = {
    "bilan": tool_bilan, "aujourdhui": tool_aujourdhui,
    "sommeil": tool_sommeil, "tendances": tool_tendances,
    "seances": tool_seances, "entrainements": tool_entrainements,
    "coeur": tool_coeur, "programme": tool_programme,
    "repas": tool_repas, "progres": tool_progres, "donnees": tool_donnees,
    "donnees_brutes": tool_donnees_brutes,
}
WRITERS: dict[str, Callable] = {
    "noter_nuit": write_noter_nuit, "regler_niveau": write_regler_niveau,
    "declarer_malade": write_declarer_malade,
    "declarer_seance": write_declarer_seance,
    "regler_objectif": write_regler_objectif,
    "regler_parametre": write_regler_parametre,
    "regler_planning": write_regler_planning,
    "seance_du_jour": write_seance_du_jour,
}


def context_for(conn: sqlite3.Connection, user_id: int, username: str) -> dict:
    """Everything a tool may know about the caller, from the session."""
    language = db.get_setting(conn, user_id, "language") or "fr"
    return {
        "user_id": user_id, "username": username,
        "today": dt.datetime.now(metrics.local_tz(conn, user_id)).date().isoformat(),
        "language": language if language in ("fr", "en") else "fr",
    }


def run_tool(user_id: int, username: str, name: str, args: Any,
             *, undo: bool = False,
             connect: Callable[[], sqlite3.Connection] = None) -> dict:
    """Run one tool for one account, on its own connection.

    Blocking (sqlite): the web route runs it in a thread.

    Returns:
        dict: ``result`` (the compact JSON string for the model), ``ok``,
        ``write`` (bool), and for a write ``data`` and ``undo``.
    """
    connect = connect or db.connect
    if not isinstance(args, dict):
        args = {}
    conn = connect()
    try:
        ctx = context_for(conn, user_id, username)
        if name in READERS:
            data = READERS[name](conn, ctx, args)
            return {"ok": True, "write": False, "result": fit(data), "args": args}
        if name in WRITERS:
            data, undo_call = WRITERS[name](conn, ctx, args, undo=undo)
            return {"ok": True, "write": True, "result": fit(data),
                    "data": data, "undo": undo_call, "args": args}
        raise ToolError(f"unknown tool {name}; use one of {sorted(READERS) + sorted(WRITERS)}")
    except ToolError as error:
        return {"ok": False, "write": name in WRITERS,
                "result": _json({"error": str(error)}), "args": args}
    except Exception as error:  # noqa: BLE001 -- the model hears about it
        return {"ok": False, "write": name in WRITERS,
                "result": _json({"error": f"{type(error).__name__}: {error}"[:300]}),
                "args": args}
    finally:
        conn.close()


# --- Context budget ----------------------------------------------------------

class TooLong(ValueError):
    """The question alone does not fit the context: HTTP 413."""


def tokens(obj) -> int:
    """Rough token count, about 4 chars per token."""
    size = len(obj) if isinstance(obj, str) else len(_json(obj))
    return size // CHARS_PER_TOKEN + 1


def budget_tokens() -> int:
    """What the prompt may use: the context minus the answer's room."""
    return NUM_CTX - REPLY_TOKENS


def clean_history(raw: Any) -> list[dict]:
    """The browser's conversation, reduced to plain user/assistant text.

    Raises:
        TooLong: the latest question is over ``MAX_USER_CHARS``.
        ValueError: malformed (no messages, last one not the user's).
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("messages must be a non-empty list")
    history = []
    for item in raw[-MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue
        role, content = item.get("role"), item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            history.append({"role": role, "content": content[:MAX_USER_CHARS * 2]})
    if not history or history[-1]["role"] != "user":
        raise ValueError("the last message must be the user's")
    if len(history[-1]["content"]) > MAX_USER_CHARS:
        raise TooLong("question too long")
    return history


def trim(system: str, history: list[dict], tools: list | None = None) -> list[dict]:
    """System prompt + as much recent history as fits the budget.

    Oldest messages go first; the latest question always stays.

    Raises:
        TooLong: system + tools + the latest question alone overflow.
    """
    fixed = tokens(system) + (tokens(tools) if tools else 0)
    room = budget_tokens() - fixed - tokens(history[-1]["content"]) - 4
    if room < 0:
        raise TooLong("question too long for the context")
    kept = [history[-1]]
    for message in reversed(history[:-1]):
        cost = tokens(message["content"]) + 4
        if cost > room:
            break
        room -= cost
        kept.insert(0, message)
    # Never open on an assistant turn without its question.
    while len(kept) > 1 and kept[0]["role"] != "user":
        kept.pop(0)
    return [{"role": "system", "content": system}, *kept]


def shrink_turn(messages: list[dict], tools: list | None, start: int) -> None:
    """Keep a growing tool loop inside the context, in place.

    First the oldest tool results of this turn are replaced by a stub,
    then whole earlier exchanges (before ``start``, the current
    question) are dropped.
    """
    def total():
        return tokens(messages) + (tokens(tools) if tools else 0)

    stub = _json({"retire": "resultat retire pour tenir dans le contexte ; "
                            "rappelle l'outil si besoin"})
    for message in messages[start:]:
        if total() <= budget_tokens():
            return
        if message.get("role") == "tool" and message["content"] != stub:
            message["content"] = stub
    while total() > budget_tokens() and start > 1:
        del messages[1]
        start -= 1


# --- The Ollama loop -----------------------------------------------------------

def _tool_calls(message: dict) -> list[tuple[str, dict]]:
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        args = function.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"_invalid": args}
        calls.append((name, args if isinstance(args, dict) else {}))
    return calls


def card_label(name: str, args: dict, language: str) -> str:
    """The small "source" chip shown under an answer."""
    label = TOOL_LABELS.get(language, TOOL_LABELS["fr"]).get(name, name)
    day = str(args.get("jour") or "")
    day = {"aujourdhui": "aujourd'hui" if language != "en" else "today",
           "hier": "hier" if language != "en" else "yesterday"}.get(day, day)
    detail = args.get("domaine") or args.get("metrique") or day or None
    if args.get("jours"):
        unit = "j" if language == "fr" else "d"
        detail = f"{detail + ' · ' if detail else ''}{args['jours']} {unit}"
    return f"{label} · {detail}" if detail else label


def action_label(name: str, data: dict, language: str) -> str:
    """The action chip: what changed, in a few words."""
    fr = language != "en"
    if name == "noter_nuit":
        return (f"Nuit notée : {data['label']}" if fr
                else f"Night logged: {data['ressenti']}")
    if name == "regler_niveau":
        return (f"Niveau du soir : {data['avant']} → {data['niveau']}" if fr
                else f"Tonight's level: {data['avant']} → {data['niveau']}")
    if name == "declarer_malade":
        if data.get("malade_jusqu_au"):
            return (f"Malade jusqu'au {data['malade_jusqu_au']}" if fr
                    else f"Ill until {data['malade_jusqu_au']}")
        return "Plus malade" if fr else "No longer ill"
    if name == "declarer_seance":
        return (f"{data['jour']} : {data['fait']}")
    if name == "regler_objectif":
        return (f"{data['objectif']} : {data['avant'] or '—'} → "
                f"{data['valeur'] or '—'}")
    if name == "regler_parametre":
        return f"{data['parametre']} : {data['avant'] or '—'} → {data['valeur'] or '—'}"
    if name == "regler_planning":
        return (f"{data['jour'].capitalize()} : {data['seance']}"
                + (f" à {data['heure']}" if data.get("heure") else ""))
    if name == "seance_du_jour":
        return str(data.get("fait") or data.get("envoye") or data.get("retour")
                   or data.get("annule") or "")[:80]
    return name


def make_client():
    """The HTTP client for Ollama (the self-check swaps in a fake)."""
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(DEADLINE_SEC, connect=10))


async def chat_events(
    user_id: int, username: str, history: list[dict], *,
    client=None, run=None, is_disconnected=None,
) -> AsyncIterator[dict]:
    """Answer the last question of ``history``, as a stream of events.

    Parameters:
        user_id / username: From the session -- the only identity any
            tool ever sees.
        history: ``clean_history`` output.
        client: An ``httpx.AsyncClient`` (tests pass one wired to a fake
            Ollama); one is created otherwise.
        run: Async ``(name, args) -> run_tool result``; defaults to
            ``run_tool`` in a worker thread.
        is_disconnected: Async callable; True stops before the next round.

    Yields:
        dict: ``{"type": "status"|"tool"|"card"|"action"|"delta"|
        "error"|"done", ...}``. Always ends with ``done``.
    """
    import httpx
    from starlette.concurrency import run_in_threadpool

    conn = db.connect()
    try:
        ctx = context_for(conn, user_id, username)
    finally:
        conn.close()
    language = ctx["language"]
    if run is None:
        async def run(name, args):
            return await run_in_threadpool(run_tool, user_id, username, name, args)
    own_client = client is None
    if own_client:
        client = make_client()
    system = system_prompt(language, username, ctx["today"])
    try:
        messages = trim(system, history, TOOLS)
    except TooLong:
        yield {"type": "error", "text": text(language, "too_long")}
        yield {"type": "done"}
        return
    start = len(messages) - 1
    answered = False
    need_break = False  # text before a tool call, more text after it
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DEADLINE_SEC

    async def within(awaitable):
        """Await under the answer's overall deadline. (No
        asyncio.timeout() around the loop: this generator yields, and a
        timeout spanning a yield would fire in the consumer's code.)"""
        remaining = deadline - loop.time()
        if remaining <= 0:
            getattr(awaitable, "close", lambda: None)()
            raise TimeoutError
        return await asyncio.wait_for(awaitable, remaining)

    try:
        yield {"type": "status", "text": text(language, "thinking")}
        for round_no in range(MAX_ROUNDS):
            if is_disconnected and await is_disconnected():
                return
            final = round_no == MAX_ROUNDS - 1
            body = {
                "model": MODEL, "messages": messages, "stream": True,
                "think": False,
                "options": {"num_ctx": NUM_CTX, "num_predict": REPLY_TOKENS},
            }
            if final:
                body["messages"] = messages + [
                    {"role": "system", "content": text(language, "final_round")}]
            else:
                body["tools"] = TOOLS
            content, calls = "", []
            request = client.build_request("POST", f"{OLLAMA_URL}/api/chat",
                                           json=body)
            response = await within(client.send(request, stream=True))
            try:
                if response.status_code != 200:
                    detail = (await within(response.aread()))[:200]
                    raise RuntimeError(
                        f"ollama {response.status_code}: "
                        f"{detail.decode('utf-8', 'replace')}")
                lines = response.aiter_lines()
                while True:
                    try:
                        line = await within(lines.__anext__())
                    except StopAsyncIteration:
                        break
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(f"ollama: {chunk['error']}")
                    message = chunk.get("message") or {}
                    piece = message.get("content") or ""
                    if piece:
                        if need_break and not content:
                            yield {"type": "delta", "text": "\n\n"}
                        content += piece
                        yield {"type": "delta", "text": piece}
                    calls.extend(_tool_calls(message))
                    if chunk.get("done"):
                        break
            finally:
                # Closing the response is what stops Ollama generating:
                # on the stop button, the deadline, or the end.
                await response.aclose()
            if content.strip():
                answered = need_break = True
            if not calls or final:
                break
            messages.append({"role": "assistant", "content": content,
                             "tool_calls": [
                                 {"function": {"name": n, "arguments": a}}
                                 for n, a in calls]})
            for name, args in calls:
                yield {"type": "tool", "name": name,
                       "label": card_label(name, args, language)}
                result = await within(run(name, args))
                messages.append({"role": "tool", "tool_name": name,
                                 "content": result["result"]})
                if result["write"]:
                    if result["ok"]:
                        yield {"type": "action", "tool": name, "ok": True,
                               "label": action_label(name, result["data"], language),
                               "undo": result["undo"]}
                    else:
                        yield {"type": "action", "tool": name, "ok": False,
                               "label": card_label(name, args, language),
                               "undo": None}
                else:
                    yield {"type": "card", "tool": name, "ok": result["ok"],
                           "label": card_label(name, args, language)}
            shrink_turn(messages, TOOLS, start)
        if not answered:
            yield {"type": "error", "text": text(language, "empty")}
    except TimeoutError:
        yield {"type": "error", "text": text(language, "deadline")}
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        print(f"assistant: user {user_id}: {error}")
        yield {"type": "error", "text": text(language, "unreachable")}
    finally:
        if own_client:
            await client.aclose()
    yield {"type": "done"}


if __name__ == "__main__":
    # The tool loop, budget and protocol with a fake Ollama; the tools
    # themselves on a seeded database: tests/selfcheck_assistant.py.
    assert "tutoies" in FR_SYSTEM_PROMPT and "medecin" in FR_SYSTEM_PROMPT
    assert "doctor" in EN_SYSTEM_PROMPT
    prompt = system_prompt("fr", "lea", "2026-11-02")
    assert "lundi 2026-11-02" in prompt and "lea" in prompt
    assert system_prompt("de", "x", "2026-11-02").startswith("Tu es")
    # Tool names are unique, and every write is listed as one.
    names = [t["function"]["name"] for t in TOOLS]
    assert len(names) == len(set(names)) == len(READERS) + len(WRITERS)
    assert set(WRITERS) == WRITE_NAMES and set(READERS).isdisjoint(WRITERS)

    # fit(): valid JSON under the cap, biggest part shrunk first.
    big = {"keep": "x", "nights": [{"n": i, "t": "y" * 50} for i in range(200)]}
    out = fit(big, 2500)
    assert len(out) <= 2500 and json.loads(out)["keep"] == "x"
    assert json.loads(out)["tronque"] is True
    assert fit({"a": 1}) == '{"a":1}'

    # Series summary: direction from code, with a stable band.
    flat = {_plus("2026-10-01", k): 80.0 + (k % 2) * 0.1 for k in range(30)}
    assert _series_summary(flat, "kg", 0.2)["direction"] == "stable"
    down = {_plus("2026-10-01", k): 80.0 - 0.1 * k for k in range(30)}
    summary = _series_summary(down, "kg", 0.2)
    assert summary["direction"] == "baisse" and summary["min"] == 77.1, summary
    assert _series_summary({}, "kg", 0.2) == {"jours_avec_donnee": 0}

    # Arguments: strings coerced, ranges enforced, days bounded.
    assert _int({"jours": "9"}, "jours", 7, 1, 14) == 9
    assert _int({"jours": 99}, "jours", 7, 1, 14) == 14
    for bad in ({"niveau": "x"}, {"niveau": 11}, {}):
        try:
            _int(bad, "niveau", None, 0, 10, clamp=False)
            raise AssertionError(bad)
        except ToolError:
            pass
    assert _day({"jour": "hier"}, "jour", "2026-11-02", 7) == "2026-11-01"
    for bad in ("2026-11-03", "2026-01-01", "demain"):
        try:
            _day({"jour": bad}, "jour", "2026-11-02", 7)
            raise AssertionError(bad)
        except ToolError:
            pass

    # History: only user/assistant text, last must be the user's.
    assert clean_history([{"role": "system", "content": "hack"},
                          {"role": "user", "content": " salut "}]) == [
        {"role": "user", "content": "salut"}]
    for bad in ([], [{"role": "assistant", "content": "x"}], "x"):
        try:
            clean_history(bad)
            raise AssertionError(bad)
        except TooLong:
            raise AssertionError("not a TooLong")
        except ValueError:
            pass
    try:
        clean_history([{"role": "user", "content": "x" * (MAX_USER_CHARS + 1)}])
        raise AssertionError("expected TooLong")
    except TooLong:
        pass

    # Trimming: the oldest go first, the question always stays.
    long_history = [{"role": r, "content": f"{i} " + "z" * 3000}
                    for i in range(20) for r in ("user", "assistant")]
    long_history.append({"role": "user", "content": "question"})
    kept = trim("sys", long_history, TOOLS)
    assert kept[0]["role"] == "system" and kept[-1]["content"] == "question"
    assert kept[1]["role"] == "user" and 2 < len(kept) < len(long_history)
    assert tokens(kept) + tokens(TOOLS) <= budget_tokens()
    try:
        trim("s" * (NUM_CTX * CHARS_PER_TOKEN), long_history)
        raise AssertionError("expected TooLong")
    except TooLong:
        pass
    # In-loop shrinking stubs this turn's tool results first.
    turn = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
            *[{"role": "tool", "content": "r" * 9000} for _ in range(5)]]
    shrink_turn(turn, TOOLS, 1)
    assert tokens(turn) + tokens(TOOLS) <= budget_tokens()
    assert "retire" in turn[2]["content"]

    # The lock: one answer per account.
    assert try_acquire(1) and not try_acquire(1) and try_acquire(2)
    release(1)
    assert try_acquire(1)
    release(1), release(2), release(2)

    print("assistant.py: all checks passed (no live model call made)")
