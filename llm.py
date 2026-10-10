#!/usr/bin/env python3
"""Ask an LLM to write the daily coaching message.

The split is the one the whole project keeps: code computes and decides,
the model only writes. That split used to stop at the numbers -- the
model still received the full payload and a page of rules and chose what
mattered, which produced an audit-style message with six labelled
sections. It now goes one step further: ``coach_brief.build_brief``
decides the day's angle and the single point worth making about
yesterday (applying every data-quality caveat), and this module hands
the model that small brief with a short prompt about voice -- warm,
encouraging, understanding, like a friend who coaches. The prompt
forbids inventing figures, and caps the message at a few short
paragraphs.

Bilingual: ``payload["language"]`` ("fr" or "en") picks the prompt. The
provider is switchable: the default reuses the Claude subscription CLI
(no pay-per-token billing); set LLM_PROVIDER=anthropic_api to call the
Anthropic API directly instead (needs ANTHROPIC_API_KEY and the
`anthropic` package).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import coach_brief

FR_SYSTEM_PROMPT = (
    "Tu es le coach sportif et nutrition d'un ami proche : chaleureux, "
    "bienveillant, encourageant, avec un brin d'humour. Tu le tutoies. "
    "Tu ecris le message qu'il lit sur son telephone le matin, avant "
    "de commencer sa journee.\n"
    "Tu recois un petit JSON, le brief. Tout y est DEJA decide et "
    "verifie : tu n'as rien a trier, calculer ou controler, seulement "
    "a bien l'ecrire.\n"
    "- angle : ce dont parle le message (rest = jour de repos, "
    "recovery = marche de recuperation, skipped_yesterday, "
    "green_streak, train).\n"
    "- body : comment il va (sommeil, batterie, variabilite "
    "cardiaque, recuperation, statut). Pioche 1 ou 2 elements, jamais "
    "tous. illness_signs = signes a surveiller : jamais un diagnostic "
    "('tu es malade' est interdit ; 'ton corps a l'air de lutter' est "
    "ok), et s'il y a de vrais symptomes, dis de consulter. "
    "tired_muscles = muscles encore fatigues par une activite recente "
    "(cause entre parentheses) : dis que c'est pour ca que la seance ne "
    "monte pas aujourd'hui, et que c'est normal.\n"
    "- session : ce qu'il fait aujourd'hui. Reprends values (vitesse, "
    "pente, duree) TELS QUELS. type=rest : aucune seance ; dis ce qui "
    "est remplace (replaces) et pourquoi (why), presente le repos "
    "comme la bonne decision et pas comme un echec, et ne propose que "
    "sommeil, eau et une petite marche facultative. type=recovery : "
    "une marche tranquille, volontairement plus legere que prevu. "
    "deload=true : semaine volontairement plus legere, c'est voulu. "
    "Pour un circuit, moves donne les mouvements du soir deja nommes et "
    "chiffres : cites-en 2 ou 3 tels quels. level_reason dit pourquoi la "
    "seance monte, reste pareille ou baisse : redis-le en clair en une "
    "demi-phrase, sans son vocabulaire interne. next_rung est la "
    "prochaine marche (un mouvement plus dur) : mentionne-la seulement "
    "comme un objectif motivant, en une demi-phrase.\n"
    "- yesterday : UN seul point sur hier. protein_short, calories_over "
    "ou water_short : dis-le simplement et donne 1 geste concret "
    "(ex. 2 oeufs et un yaourt grec). older_day : cite la date de CE "
    "jour-la, jamais comme si c'etait hier. not_synced ou log_partial : "
    "n'en tire AUCUNE conclusion, ne dis pas qu'il a mal mange, passe "
    "a autre chose. on_track : un compliment. topic=movement : un mot "
    "sur ce flag, sans reproche, avec une idee simple. topic=none : ne "
    "parle pas d'hier.\n"
    "- skipped : s'il existe, une phrase bienveillante : le fait, "
    "neutre (ne dis pas que c'est bien, ni que c'est mal), puis "
    "next_step ; jamais de culpabilisation. green_streak : "
    "felicite la serie.\n"
    "- desk : 1 ou 2 gestes silencieux pour son bureau PARTAGE. Nomme "
    "chaque geste (name) et resume how en UNE phrase, sans changer le "
    "geste ni ses chiffres ; dis quand le faire (when) ; ajoute note "
    "si elle existe. gentle_only = "
    "journee douce, dis-le. Si yesterday est un point mouvement, une "
    "seule phrase sur hier puis le geste dans le meme paragraphe : ne "
    "repete pas deux fois le meme conseil.\n"
    "- help : l'aide concrete, DEJA chiffree. food.items = quoi manger "
    "(name, qty) : reprends aliments et quantites tels quels, n'en "
    "invente jamais d'autres. water.glasses (et habit). lighter_dinner."
    "how. care (jour de repos) : drink_ml a boire, resume_when, et cite "
    "see_doctor_if en une phrase calme, jamais alarmiste.\n"
    "- next_step : la suite concrete. La derniere phrase du message "
    "s'appuie dessus, reformulee avec tes mots.\n"
    "- weather : une demi-phrase au plus, facultatif.\n"
    "- weekly (dimanche seulement) : un paragraphe en plus, UN seul "
    "point chiffre repris tel quel (weight_trend = moyenne de la "
    "derniere semaine contre celle d'avant). recent_move : nomme-le et ne parle "
    "jamais de plateau. plateau : dis-le et propose un ajustement. "
    "recalibration : donne suggested_daily_calorie_adjustment_kcal tel "
    "quel. Sans weekly, ne parle pas du poids.\n"
    "Le budget calories/macros part dans une notification a part : ne "
    "le repete pas.\n"
    "Style : 4 courts paragraphes separes par une ligne vide (5 le "
    "dimanche), environ 100 mots et 120 mots au maximum, texte brut : pas de titre, "
    "pas d'etiquette, pas de liste. Ouvre sur son etat ou sur une "
    "vraie salutation. Phrases courtes et naturelles, comme un message "
    "a un ami. Un emoji au maximum. Sois encourageant et "
    "comprehensif : reconnais l'effort, normalise les jours difficiles, "
    "termine sur next_step reformule : une suite concrete, jamais une "
    "formule generique. Phrases toutes faites INTERDITES : 'ca arrive a "
    "tout le monde', 'rien de grave', 'continue comme ca', 'tu geres', "
    "'elle ne va nulle part'. Jamais de ton "
    "militaire ou moralisateur, jamais de remarque sur son corps ou son "
    "poids. Aucun vocabulaire interne : ne dis jamais statut, niveau, "
    "rouge, vert, deload -- dis 'ton corps demande du calme', 'une "
    "belle serie de bons matins'. Chaque chiffre vient du JSON, jamais invente, 3 au maximum "
    "dans tout le message. Pas de sigle technique, pas de jargon.\n"
    "Exemples de TON, a ne jamais recopier :\n"
    "(repos) Salut ! Aujourd'hui on leve le pied : ton corps a besoin "
    "de toute son energie pour recuperer, et c'est la meilleure "
    "decision de la journee. Pas de tapis, de l'eau, du sommeil, et une "
    "petite marche seulement si l'envie vient.\n\n"
    "Au bureau, sans que personne le remarque : respiration 4-4-4-4, "
    "six cycles, les yeux sur l'ecran.\n\n"
    "Demain on voit comment tu te sens, et on reprendra par une marche "
    "avant de remonter sur le tapis. Repose-toi bien.\n"
    "(entrainement) Bonjour ! Belle forme ce matin, bon sommeil. Ce "
    "soir : tapis, 6.5 km/h, 12 % de pente, 25 minutes.\n\n"
    "Il te manquait 38 g de proteines hier : deux oeufs et un yaourt "
    "grec a midi te remettent d'aplomb.\n\n"
    "Pause bureau avant 11 h : montees sur pointes contre le bureau, "
    "20 fois, lentement.\n\n"
    "Ce soir, dis-moi sur le tableau de bord si c'etait trop facile, "
    "juste bien ou trop dur : c'est comme ca que j'ajuste la suite."
)

EN_SYSTEM_PROMPT = (
    "You are a close friend's sports and nutrition coach: warm, kind, "
    "encouraging, with a touch of humour. You write the message they "
    "read on their phone in the morning, before the day starts.\n"
    "You receive a small JSON, the brief. Everything in it is ALREADY "
    "decided and checked: you sort, compute and verify nothing, you "
    "only write it well.\n"
    "- angle: what the message is about (rest = rest day, recovery = "
    "recovery walk, skipped_yesterday, green_streak, train).\n"
    "- body: how they are (sleep, battery, heart-rate variability, "
    "recovery, status). Pick 1 or 2 items, never all. illness_signs = "
    "signs to watch: never a diagnosis ('you are sick' is forbidden; "
    "'your body seems to be fighting something' is fine), and if real "
    "symptoms show up, say to see a doctor. tired_muscles = muscles "
    "still tired from a recent activity (cause in brackets): say that "
    "is why the session does not step up today, and that it is normal.\n"
    "- session: what they do today. Quote values (speed, incline, "
    "duration) AS-IS. type=rest: no session; say what is replaced "
    "(replaces) and why (why), present rest as the right call and not "
    "a failure, and only suggest sleep, water and an optional easy "
    "walk. type=recovery: an easy walk, deliberately lighter than "
    "planned. deload=true: a deliberately lighter week, by design. "
    "For a circuit, moves lists tonight's moves already named and "
    "counted: quote 2 or 3 of them as-is. level_reason says why the "
    "session goes up, stays or goes down: say it plainly in half a "
    "sentence, without its internal vocabulary. next_rung is the next "
    "step (a harder move): mention it only as a motivating goal, in "
    "half a sentence.\n"
    "- yesterday: ONE point about yesterday. protein_short, "
    "calories_over or water_short: say it plainly and give 1 concrete "
    "fix (e.g. 2 eggs and a Greek yoghurt). older_day: name THAT day's "
    "date, never as if it were yesterday. not_synced or log_partial: "
    "draw NO conclusion, never say they ate badly, move on. on_track: "
    "a compliment. topic=movement: a word on that flag, no blame, one "
    "simple idea. topic=none: do not mention yesterday.\n"
    "- skipped: if present, one kind sentence: the fact, neutral "
    "(do not call it good or bad), then next_step; never guilt. green_streak: congratulate the streak.\n"
    "- desk: 1 or 2 silent moves for their SHARED office. Name each "
    "move (name) and sum up how in ONE sentence, without changing the "
    "move or its figures; say when to do it (when); add note if "
    "present. gentle_only = easy "
    "day, say so. If yesterday is a movement point, one sentence about "
    "it then the move in the same paragraph: never give the same tip "
    "twice.\n"
    "- help: the concrete help, ALREADY sized. food.items = what to eat "
    "(name, qty): quote foods and quantities as-is, never invent "
    "others. water.glasses (and habit). lighter_dinner.how. care (rest "
    "day): drink_ml to drink, resume_when, and name see_doctor_if in "
    "one calm sentence, never alarmist.\n"
    "- next_step: the concrete way forward. The message's last "
    "sentence builds on it, put in your own words.\n"
    "- weather: half a sentence at most, optional.\n"
    "- weekly (Sunday only): one extra paragraph, ONE figure quoted "
    "as-is (weight_trend = last week's average against the week "
    "before). recent_move: name it and never speak of a plateau. "
    "plateau: say so and suggest one adjustment. recalibration: give "
    "suggested_daily_calorie_adjustment_kcal as-is. Without weekly, "
    "do not talk about weight.\n"
    "The calorie/macro budget goes out in a separate notification: do "
    "not repeat it.\n"
    "Style: 4 short paragraphs separated by a blank line (5 on "
    "Sunday), about 100 words and 120 words at the most, plain text: no headings, no "
    "labels, no lists. Open on how they are or a real greeting. Short, "
    "natural sentences, like a message to a friend. One emoji at most. "
    "Be encouraging and understanding: acknowledge effort, normalise "
    "hard days, close on next_step reworded: a concrete way forward, "
    "never a generic line. Stock phrases are FORBIDDEN: 'it happens to "
    "everyone', 'nothing serious', 'keep it up', 'you are doing well', "
    "'it is not going anywhere'. Never "
    "drill-sergeant or preachy, never a remark about their body or "
    "weight. No internal vocabulary: never say status, level, red, "
    "green, deload -- say 'your body is asking for calm', 'a lovely "
    "run of good mornings'. Every figure comes from the JSON, never invented, 3 at "
    "most in the whole message. No technical acronyms, no jargon.\n"
    "Examples of TONE, never to copy:\n"
    "(rest) Hey! Today we ease off: your body needs all its energy to "
    "recover, and that is the best call of the day. No treadmill, "
    "water, sleep, and a gentle walk only if you feel like it.\n\n"
    "At your desk, without anyone noticing: 4-4-4-4 breathing, six "
    "cycles, eyes on the screen.\n\n"
    "Tomorrow we see how you feel, and we will restart with a walk "
    "before getting back on the treadmill. Rest well.\n"
    "(training) Good morning! You look in great shape, good sleep. "
    "Tonight: treadmill, 6.5 km/h, 12% incline, 25 minutes.\n\n"
    "You were 38 g short on protein yesterday: two eggs and a Greek "
    "yoghurt at lunch will sort you out.\n\n"
    "Desk break before 11: calf raises against the desk, 20 times, "
    "slowly.\n\n"
    "Tonight, tell me on the dashboard whether it was too easy, just "
    "right or too hard: that is how I adjust what comes next."
)

SYSTEM_PROMPTS = {"fr": FR_SYSTEM_PROMPT, "en": EN_SYSTEM_PROMPT}
DEFAULT_LANGUAGE = "fr"


def _build_prompt(payload: dict) -> str:
    """Concatenate the right-language system prompt with the brief.

    Parameters:
        payload (dict): ``coach_brief.build_brief`` output;
            ``payload["language"]`` ("fr"/"en") picks the prompt,
            defaulting to French.

    Returns:
        str: Full prompt text sent to the LLM.
    """
    language = payload.get("language", DEFAULT_LANGUAGE)
    system_prompt = SYSTEM_PROMPTS.get(language, FR_SYSTEM_PROMPT)
    label = "Brief"
    return f"{system_prompt}\n\n{label}:\n{json.dumps(payload)}"


# Model for the morning message. Unset: the claude CLI uses the
# subscription's default and the API path uses API_DEFAULT_MODEL. The
# brief is small and fully decided, so this is a writing task:
# claude-sonnet-5-5 does it well, claude-haiku-4-5 is fine and cheapest.
COACH_MODEL = os.environ.get("COACH_MODEL", "").strip()
API_DEFAULT_MODEL = "claude-sonnet-5-5"
# Server-side refusal fallbacks are only offered for these models.
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
                    "claude-fable-5-1")
# The CLI prints these (exit code 0 on some versions) when the
# subscription's quota is spent: treat them as a failure, never as the
# message itself.
_LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit",
                  "credit balance is too low", "out of extra usage")


class ProviderUnavailable(RuntimeError):
    """A provider could not produce a message (quota, network...)."""


def _coach_claude_cli(payload: dict) -> str:
    """Ask Claude via the local CLI (subscription OAuth, no API key).

    Parameters:
        payload (dict): See ``_build_prompt``.

    Returns:
        str: Plain-text coaching message.

    Raises:
        ProviderUnavailable: CLI failure or spent quota.
    """
    claude = (
        shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    )
    command = [claude, "-p", _build_prompt(payload)]
    if COACH_MODEL:
        command += ["--model", COACH_MODEL]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProviderUnavailable(f"claude CLI: {error}") from error
    text = result.stdout.strip()
    if result.returncode != 0:
        raise ProviderUnavailable(
            f"claude CLI failed: {(result.stderr or text)[:500]}"
        )
    if not text or (
        len(text) < 300
        and any(marker in text.lower() for marker in _LIMIT_MARKERS)
    ):
        raise ProviderUnavailable(f"claude CLI: {text[:200] or 'empty'}")
    return text


def _coach_anthropic_api(payload: dict) -> str:
    """Ask Claude via the Anthropic API (pay-per-token).

    Parameters:
        payload (dict): See ``_build_prompt``.

    Returns:
        str: Plain-text coaching message.

    Raises:
        ProviderUnavailable: API error, refusal, or truncated output.
    """
    import anthropic  # local import: optional dependency

    model = (
        COACH_MODEL or os.environ.get("ANTHROPIC_MODEL") or API_DEFAULT_MODEL
    )
    request = {
        "model": model, "max_tokens": 2000,
        "messages": [{"role": "user", "content": _build_prompt(payload)}],
    }
    try:
        client = anthropic.Anthropic()
        if model in _FALLBACK_MODELS:
            # A safety decline is retried on another model server-side.
            message = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default", **request,
            )
        else:
            message = client.messages.create(**request)
    except anthropic.APIError as error:
        raise ProviderUnavailable(f"Anthropic API: {error}") from error
    if message.stop_reason in ("refusal", "max_tokens"):
        raise ProviderUnavailable(f"Anthropic API: {message.stop_reason}")
    text = "".join(
        block.text for block in message.content if block.type == "text"
    ).strip()
    if not text:
        raise ProviderUnavailable("Anthropic API: empty reply")
    return text


def _coach_openai_compatible(payload: dict) -> str:
    """Any OpenAI-compatible chat endpoint: Ollama, LM Studio, OpenRouter...

    The way out when there is no Claude quota left: a local model on the
    server (Ollama: ``OPENAI_COMPAT_BASE_URL=http://host:11434/v1``) or
    any hosted endpoint with a key.

    Environment:
        OPENAI_COMPAT_BASE_URL: Base URL ending in ``/v1``.
        OPENAI_COMPAT_MODEL: Model name the endpoint serves.
        OPENAI_COMPAT_API_KEY: Bearer key, optional (Ollama needs none).
    """
    import requests

    base = os.environ.get("OPENAI_COMPAT_BASE_URL", "").rstrip("/")
    model = os.environ.get("OPENAI_COMPAT_MODEL", "")
    if not base or not model:
        raise ProviderUnavailable(
            "openai_compatible: set OPENAI_COMPAT_BASE_URL and "
            "OPENAI_COMPAT_MODEL"
        )
    headers = {}
    if os.environ.get("OPENAI_COMPAT_API_KEY"):
        headers["Authorization"] = (
            f"Bearer {os.environ['OPENAI_COMPAT_API_KEY']}"
        )
    try:
        response = requests.post(
            f"{base}/chat/completions", headers=headers, timeout=300,
            json={"model": model, "max_tokens": 800, "messages": [
                {"role": "user", "content": _build_prompt(payload)},
            ]},
        )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()
    except (requests.RequestException, KeyError, IndexError, ValueError,
            TypeError) as error:
        raise ProviderUnavailable(f"openai_compatible: {error}") from error
    if not text:
        raise ProviderUnavailable("openai_compatible: empty reply")
    return text


def _coach_template(payload: dict) -> str:
    """No model at all: the message assembled from the brief itself."""
    import plain_message

    text = plain_message.render_or_none(payload)
    if not text:
        raise ProviderUnavailable("template: could not render")
    return text


PROVIDERS = {
    "claude_cli": _coach_claude_cli,
    "anthropic_api": _coach_anthropic_api,
    "openai_compatible": _coach_openai_compatible,
    "template": _coach_template,
}
FALLBACK_NOTE = {
    "fr": "(Message simplifie : le modele habituel n'etait pas disponible "
          "ce matin.)",
    "en": "(Simplified message: the usual model was not available this "
          "morning.)",
}


def provider_chain() -> list[str]:
    """``LLM_PROVIDER`` then ``LLM_FALLBACKS``, ending with the template.

    Raises:
        ValueError: An unknown provider name, so a typo in .env fails
            loudly instead of silently landing on the template.
    """
    names = [os.environ.get("LLM_PROVIDER", "claude_cli")] + [
        name.strip() for name in os.environ.get(
            "LLM_FALLBACKS", "",
        ).split(",") if name.strip()
    ] + ["template"]
    unknown = [name for name in names if name not in PROVIDERS]
    if unknown:
        raise ValueError(
            f"Unknown LLM provider(s) {unknown}, expected one of "
            f"{sorted(PROVIDERS)}"
        )
    chain: list[str] = []
    for name in names:
        if name not in chain:
            chain.append(name)
    return chain


def coach(payload: dict) -> str:
    """Generate today's coaching message, falling back down the chain.

    Each provider is tried in turn (``provider_chain``); the template
    one cannot run out of quota, so a message always comes out. When it
    is the template that answers after a failure, the message says so.

    Parameters:
        payload (dict): The full ``coach_payload.build_payload`` output,
            plus ``language`` ("fr"/"en"). It is reduced to a brief by
            ``coach_brief.build_brief`` before the model sees it.

    Returns:
        str: Plain-text coaching message.

    Raises:
        ValueError: Unknown provider name in the configuration.
        ProviderUnavailable: Every provider failed, the template too.
    """
    chain = provider_chain()
    brief = coach_brief.build_brief(payload)
    errors = []
    for name in chain:
        try:
            text = PROVIDERS[name](brief)
        except Exception as error:  # noqa: BLE001 -- try the next one
            errors.append(f"{name}: {error}")
            print(f"llm: {name} unavailable -- {error}", file=sys.stderr)
            continue
        if name == "template" and errors:
            note = FALLBACK_NOTE.get(brief.get("language"), FALLBACK_NOTE["fr"])
            text = f"{text}\n\n{note}"
        return text
    raise ProviderUnavailable("; ".join(errors))


if __name__ == "__main__":
    prompt_fr = _build_prompt({"date": "2026-07-13", "angle": "train"})
    assert '"date": "2026-07-13"' in prompt_fr
    assert "Brief" in prompt_fr and "ami proche" in prompt_fr
    prompt_en = _build_prompt({"date": "2026-07-13", "language": "en"})
    assert "Brief" in prompt_en and "close friend" in prompt_en

    # Every top-level field the brief can carry is explained to the
    # model, in both languages -- a field the prompt never mentions is
    # one the model improvises around -- and the prompt names nothing
    # the brief never carries.
    full = coach_brief.build_brief({
        "date": "2026-10-04", "language": "fr",
        "wellness_today": {"sleep_score": 80},
        "today_session": {"type": "treadmill", "status": "green"},
        "weekly_progress": {"weight_trend_14d": {"delta": -0.5}},
        "movement_yesterday": {"flags": []},
        "statuses_last_7_days": [
            {"date": "d", "status": "green"} for _ in range(3)
        ],
        "session_skipped_yesterday": {"date": "d", "session_type": "x"},
        "desk_break": {"items": [
            {"name": "n", "how": "h", "seconds": 5},
        ]},
        "weather_today": {"temp_max_c": 12},
    })
    for prompt in (FR_SYSTEM_PROMPT, EN_SYSTEM_PROMPT):
        for key in full:
            if key not in ("language", "date"):
                assert key in prompt, key
        for key in ("angle", "body", "session", "yesterday", "skipped",
                    "green_streak", "desk", "weather", "weekly"):
            assert key in prompt, key

    # The caveats now live in the brief, so the prompt must keep telling
    # the model what to do with each of them -- the fixes that took
    # several mornings to find.
    for token in ("older_day", "not_synced", "log_partial",
                  "recent_move", "diagnostic", "replaces", "moves",
                  "level_reason", "next_rung", "tired_muscles"):
        assert token in FR_SYSTEM_PROMPT, token
    for token in ("older_day", "not_synced", "log_partial",
                  "recent_move", "diagnosis", "replaces", "moves",
                  "level_reason", "next_rung", "tired_muscles"):
        assert token in EN_SYSTEM_PROMPT, token

    # The old report format is gone: no section labels, no hard
    # tone, and the length is capped well under the old 260 words.
    for label in ("AUJOURD'HUI :", "CONSEIL :", "NUTRITION :", "PROGRES :",
                  "VIE :", "BUREAU :"):
        assert label not in FR_SYSTEM_PROMPT, label
    for label in ("TODAY:", "TIP:", "NUTRITION:", "PROGRESS:", "LIFE:",
                  "OFFICE:"):
        assert label not in EN_SYSTEM_PROMPT, label
    assert "exigeant" not in FR_SYSTEM_PROMPT
    assert "120 mots" in FR_SYSTEM_PROMPT and "120 words" in EN_SYSTEM_PROMPT

    # The stock phrases the prompt forbids are not in its own examples.
    for banned in ("ca arrive a tout le monde", "rien de grave",
                   "continue comme ca", "tu geres"):
        head, _, examples = FR_SYSTEM_PROMPT.partition("Exemples de TON")
        assert banned not in examples, banned
    for banned in ("it happens to everyone", "nothing serious",
                   "keep it up", "you are doing well"):
        head, _, examples = EN_SYSTEM_PROMPT.partition("Examples of TONE")
        assert banned not in examples, banned

    # The examples that set the tone fit the length the prompt asks
    # for, so the model is never shown a message over its own limit.
    for prompt, rest_tag, train_tag in (
        (FR_SYSTEM_PROMPT, "(repos)", "(entrainement)"),
        (EN_SYSTEM_PROMPT, "(rest)", "(training)"),
    ):
        rest_example, train_example = prompt.split(rest_tag)[1].split(
            train_tag
        )
        assert 40 <= len(rest_example.split()) <= 120, len(rest_example.split())
        assert 40 <= len(train_example.split()) <= 120, len(train_example.split())

    # Unknown language falls back to French rather than erroring.
    prompt_fallback = _build_prompt({"language": "de"})
    assert prompt_fallback.startswith(FR_SYSTEM_PROMPT[:20])

    # coach() hands the provider the brief, not the whole payload.
    seen = []
    PROVIDERS["spy"] = lambda brief: seen.append(brief) or "ok"
    os.environ["LLM_PROVIDER"] = "spy"
    try:
        assert coach({
            "date": "2026-10-05", "language": "fr",
            "today_session": {"type": "rest"},
            "activities_last_7_days": [{"label": "x"}],
        }) == "ok"
        assert seen[0]["angle"] == "rest", seen
        assert "activities_last_7_days" not in seen[0], seen
    finally:
        del PROVIDERS["spy"]
        del os.environ["LLM_PROVIDER"]

    os.environ["LLM_PROVIDER"] = "nonsense"
    try:
        coach({})
        raise AssertionError("expected ValueError for LLM_PROVIDER=nonsense")
    except ValueError:
        pass
    finally:
        del os.environ["LLM_PROVIDER"]

    # The fallback chain: a spent Claude quota still produces a message.
    def _spent(brief):
        raise ProviderUnavailable("usage limit reached")

    PROVIDERS["spent"] = _spent
    PROVIDERS["backup"] = lambda brief: "message de secours"
    rest_payload = {"date": "2026-10-05", "language": "fr",
                    "today_session": {"type": "rest",
                                      "scheduled_title": "Tapis"}}
    os.environ["LLM_PROVIDER"] = "spent"
    try:
        # No fallback configured: the template answers, and says so.
        text = coach(rest_payload)
        assert "repos" in text and FALLBACK_NOTE["fr"] in text, text
        # A configured fallback answers before the template.
        os.environ["LLM_FALLBACKS"] = "backup"
        assert coach(rest_payload) == "message de secours"
        assert provider_chain() == ["spent", "backup", "template"]
        # A typo in the fallbacks fails loudly, not silently.
        os.environ["LLM_FALLBACKS"] = "bakcup"
        try:
            coach(rest_payload)
            raise AssertionError("expected ValueError for a bad fallback")
        except ValueError:
            pass
    finally:
        for key in ("LLM_PROVIDER", "LLM_FALLBACKS"):
            os.environ.pop(key, None)
        del PROVIDERS["spent"], PROVIDERS["backup"]
    # Template as the chosen provider: no "fallback" note.
    os.environ["LLM_PROVIDER"] = "template"
    try:
        assert FALLBACK_NOTE["fr"] not in coach(rest_payload)
    finally:
        del os.environ["LLM_PROVIDER"]

    # The CLI's quota message is a failure, never the morning message.
    import types as _types

    real_run = subprocess.run
    subprocess.run = lambda *a, **k: _types.SimpleNamespace(
        returncode=0, stdout="Claude AI usage limit reached|1791110400",
        stderr="")
    try:
        _coach_claude_cli({"language": "fr"})
        raise AssertionError("expected ProviderUnavailable")
    except ProviderUnavailable:
        pass
    finally:
        subprocess.run = real_run
    # COACH_MODEL reaches the CLI as --model.
    calls = []
    COACH_MODEL = "claude-haiku-4-5"
    subprocess.run = lambda cmd, **k: calls.append(cmd) or (
        _types.SimpleNamespace(returncode=0, stdout="x" * 400, stderr=""))
    try:
        _coach_claude_cli({"language": "fr"})
    finally:
        subprocess.run = real_run
        COACH_MODEL = ""
    assert calls[0][-2:] == ["--model", "claude-haiku-4-5"], calls
    # The OpenAI-compatible provider needs its endpoint configured.
    try:
        _coach_openai_compatible({"language": "fr"})
        raise AssertionError("expected ProviderUnavailable")
    except ProviderUnavailable as error:
        assert "OPENAI_COMPAT_BASE_URL" in str(error)

    print("llm.py: all checks passed (no live LLM call made)")
