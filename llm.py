#!/usr/bin/env python3
"""Ask an LLM to phrase the daily coaching message.

Same split as garmin-coach: training.py/progress.py compute the
numbers, this module only narrates around them -- the prompt
explicitly forbids inventing figures. Differences from garmin-coach's
coach.py:

1. The payload carries everything smart_sport can compute: today's
   full wellness snapshot (sleep, RHR, steps, distance, floors,
   hydration, calories burned), yesterday's nutrition/hydration vs
   computed targets with the gap, and weekly_progress (weight/body-fat
   trend, calorie balance, protein, plateau flag) -- the prompt is
   written to actually use all of it, not just today_session.
2. NUTRITION suggestions are concrete (specific foods/drinks sized to
   the computed gap), and the tone is explicitly allowed to be blunt
   when the numbers show a real miss -- still never inventing a
   number, always ending on one actionable fix.
3. Bilingual: ``payload["language"]`` ("fr" or "en") picks the system
   prompt; both are native-written, not machine-translated.
4. Provider is switchable: the default reuses the Claude subscription
   CLI trick (no pay-per-token billing); set LLM_PROVIDER=anthropic_api
   to call the Anthropic API directly instead (needs ANTHROPIC_API_KEY
   and the `anthropic` package).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

FR_SYSTEM_PROMPT = (
    "Tu es un coach sportif et nutrition direct et exigeant. "
    "L'objectif de l'athlete est la recomposition corporelle : perdre "
    "du gras et prendre du muscle. Tu recois un JSON avec : les 7 "
    "derniers jours d'activite (activities_last_7_days), le "
    "sommeil/recuperation/pas/distance/etages/calories brulees du "
    "jour (wellness_today), la nutrition d'hier si loguee "
    "(nutrition_today porte parfois les donnees d'hier selon "
    "l'appel), les objectifs et l'ecart d'hier vs cible "
    "(weekly_progress.nutrition_yesterday : targets, actual, gap -- "
    "gap positif = manque encore, negatif = deja depasse), les "
    "tendances hebdo (weekly_progress : poids, masse grasse, balance "
    "calorique, proteines, plateau), et la seance prevue ce soir "
    "(today_session).\n"
    "Reponds en FRANCAIS, texte brut, 180 mots max, en lignes :\n"
    "AUJOURD'HUI : la seance du jour (activite, duree, intensite ; "
    "pente en % si tapis), adaptee a la recuperation. Annonce le "
    "statut (vert/jaune/rouge) et reprends TELS QUELS les chiffres de "
    "today_session.values ou .note -- n'invente jamais un autre "
    "chiffre pour la seance.\n"
    "CONSEIL : un seul conseil qui renforce la seance du jour.\n"
    "NUTRITION : base-toi sur weekly_progress.nutrition_yesterday. Si "
    "gap existe, dis clairement si hier etait bon ou pas en citant le "
    "chiffre du gap (ex. '42g de proteines sous l'objectif'), puis "
    "propose 1-2 aliments ou boissons CONCRETS et dimensionnes pour "
    "corriger aujourd'hui (ex. '150g de poulet + 2 oeufs', '500ml "
    "d'eau maintenant'), jamais un conseil vague type 'mange plus de "
    "proteines'. Si rien n'est logue (targets ou actual absents), "
    "dis-le en une phrase et n'invente aucune suggestion chiffree.\n"
    "PROGRES : si weekly_progress.weight_trend_14d ou "
    "calorie_balance_7d ont des donnees, UN point chiffre dessus "
    "(delta de poids, balance calorique) repris tel quel. Si "
    "weekly_progress.plateau.plateau est vrai, dis-le et donne UN "
    "ajustement concret. Si weekly_progress.recalibration.flagged est "
    "vrai, dis que le rythme reel (actual_weekly_kg) s'ecarte de "
    "l'objectif (target_weekly_kg) et donne "
    "suggested_daily_calorie_adjustment_kcal tel quel comme piste "
    "d'ajustement. Si today_session.deload_triggered est vrai, "
    "annonce clairement la semaine de deload (3 seances rouges "
    "d'affilee -> volume reduit, c'est voulu, pas un echec). Sinon "
    "saute cette ligne.\n"
    "VIE : un conseil sommeil ou hydratation base sur wellness_today "
    "(hydration_ml_today vs hydration_target_ml, sleep_score, "
    "steps_today vs step_goal).\n"
    "Ces 5 lignes (AUJOURD'HUI/CONSEIL/NUTRITION/PROGRES/VIE) sont "
    "TOUTE la reponse -- n'ajoute jamais une 6e ligne ou un label "
    "supplementaire, meme pour resumer le ton.\n"
    "Consigne de ton (a appliquer DANS les lignes ci-dessus, jamais "
    "comme ligne separee) : sois direct et sans complaisance quand "
    "les chiffres montrent un ecart net avec les objectifs (proteines "
    "ou hydratation loin sous la cible, balance calorique en surplus "
    "alors que l'objectif est une perte de poids, plateau vrai, "
    "statut rouge) -- dis-le franchement, pas d'edulcorant, mais "
    "reste factuel (uniquement les chiffres fournis) et TOUJOURS "
    "termine par une action precise. Si les chiffres sont bons, sois "
    "positif mais reste concis, sans complaisance excessive non "
    "plus. Jamais insultant, juste sans detour.\n"
    "Le tout doit former UN plan coherent : jamais un conseil qui "
    "contredit la seance proposee. Si la recuperation est basse, "
    "tout va dans le sens du recul ; sinon, de la progression.\n"
    "Ne jamais inventer un chiffre absent du JSON. 2-3 chiffres "
    "precis maximum par ligne, pas de jargon, pas de salutations."
)

EN_SYSTEM_PROMPT = (
    "You are a direct, no-nonsense sports and nutrition coach. The "
    "athlete's goal is body recomposition: losing fat and gaining "
    "muscle. You receive a JSON with: the last 7 days of activity "
    "(activities_last_7_days), today's sleep/recovery/steps/distance/"
    "floors/calories burned (wellness_today), yesterday's logged "
    "nutrition if any, the targets and yesterday's gap vs target "
    "(weekly_progress.nutrition_yesterday: targets, actual, gap -- "
    "positive gap = still short, negative = already exceeded), "
    "weekly trends (weekly_progress: weight, body fat, calorie "
    "balance, protein, plateau), and tonight's planned session "
    "(today_session).\n"
    "Respond in ENGLISH, plain text, 180 words max, in lines:\n"
    "TODAY: today's session (activity, duration, intensity; incline "
    "% if treadmill), adapted to recovery. State the status (green/"
    "yellow/red) and reuse the EXACT figures from "
    "today_session.values or .note -- never invent a different "
    "number for the session.\n"
    "TIP: one single tip that reinforces today's session.\n"
    "NUTRITION: base this on weekly_progress.nutrition_yesterday. If "
    "gap exists, say plainly whether yesterday was on track, quoting "
    "the gap figure (e.g. '42g protein short of target'), then "
    "suggest 1-2 CONCRETE, sized foods or drinks to fix it today "
    "(e.g. '150g chicken breast + 2 eggs', '500ml water now'), never "
    "a vague 'eat more protein'. If nothing is logged (targets or "
    "actual missing), say so in one sentence and invent no sized "
    "suggestion.\n"
    "PROGRESS: if weekly_progress.weight_trend_14d or "
    "calorie_balance_7d have data, ONE figure-based point (weight "
    "delta, calorie balance), reused as-is. If "
    "weekly_progress.plateau.plateau is true, say so and give ONE "
    "concrete adjustment. If weekly_progress.recalibration.flagged is "
    "true, say the actual rate (actual_weekly_kg) has drifted from "
    "the goal (target_weekly_kg) and give "
    "suggested_daily_calorie_adjustment_kcal as-is. If "
    "today_session.deload_triggered is true, clearly call out the "
    "deload week (3 reds in a row -> reduced volume, by design, not a "
    "failure). Otherwise skip this line.\n"
    "LIFE: one sleep or hydration tip based on wellness_today "
    "(hydration_ml_today vs hydration_target_ml, sleep_score, "
    "steps_today vs step_goal).\n"
    "These 5 lines (TODAY/TIP/NUTRITION/PROGRESS/LIFE) are the WHOLE "
    "reply -- never add a 6th line or an extra label, even to "
    "summarize the tone.\n"
    "Tone guidance (apply it WITHIN the lines above, never as a "
    "separate line): be direct and blunt when the numbers show a "
    "clear miss against targets (protein or hydration far under "
    "target, calorie balance in surplus while the goal is fat loss, a "
    "real plateau, red status) -- say it plainly, no sugar-coating, "
    "but stay factual (only the numbers given) and ALWAYS end with "
    "one precise action. If the numbers are good, be positive but "
    "stay concise, no excessive coddling either. Never insulting, "
    "just straight to the point.\n"
    "The whole message must form ONE coherent plan: never a tip that "
    "contradicts the proposed session. If recovery is low, everything "
    "leans toward pulling back; otherwise, toward progression.\n"
    "Never invent a number absent from the JSON. 2-3 precise figures "
    "max per line, no jargon, no greetings."
)

SYSTEM_PROMPTS = {"fr": FR_SYSTEM_PROMPT, "en": EN_SYSTEM_PROMPT}
DEFAULT_LANGUAGE = "fr"


def _build_prompt(payload: dict) -> str:
    """Concatenate the right-language system prompt with the payload.

    Parameters:
        payload (dict): Today's session/wellness/nutrition +
            weekly_progress; ``payload["language"]`` ("fr"/"en")
            picks the prompt, defaulting to French.

    Returns:
        str: Full prompt text sent to the LLM.
    """
    language = payload.get("language", DEFAULT_LANGUAGE)
    system_prompt = SYSTEM_PROMPTS.get(language, FR_SYSTEM_PROMPT)
    label = "Athlete data" if language == "en" else "Donnees athlete"
    return f"{system_prompt}\n\n{label}:\n{json.dumps(payload)}"


def _coach_claude_cli(payload: dict) -> str:
    """Ask Claude via the local CLI (subscription OAuth, no API key).

    Parameters:
        payload (dict): See ``_build_prompt``.

    Returns:
        str: Plain-text coaching message.
    """
    claude = (
        shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    )
    result = subprocess.run(
        [claude, "-p", _build_prompt(payload)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:500]}")
    return result.stdout.strip()


def _coach_anthropic_api(payload: dict) -> str:
    """Ask Claude via the Anthropic API (pay-per-token).

    Parameters:
        payload (dict): See ``_build_prompt``.

    Returns:
        str: Plain-text coaching message.
    """
    import anthropic  # local import: optional dependency

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        max_tokens=500,
        messages=[{"role": "user", "content": _build_prompt(payload)}],
    )
    return message.content[0].text.strip()


PROVIDERS = {
    "claude_cli": _coach_claude_cli,
    "anthropic_api": _coach_anthropic_api,
}


def coach(payload: dict) -> str:
    """Generate today's coaching message.

    Parameters:
        payload (dict): Session/wellness/nutrition/progress data,
            plus ``language`` ("fr"/"en").

    Returns:
        str: Plain-text coaching message.

    Raises:
        ValueError: Unknown ``LLM_PROVIDER``.
    """
    provider = os.environ.get("LLM_PROVIDER", "claude_cli")
    if provider not in PROVIDERS:
        raise ValueError(
            f"Unknown LLM_PROVIDER {provider!r}, expected one of "
            f"{sorted(PROVIDERS)}"
        )
    return PROVIDERS[provider](payload)


if __name__ == "__main__":
    prompt_fr = _build_prompt({"date": "2026-07-13", "today_session": {}})
    assert "recomposition corporelle" in prompt_fr
    assert '"date": "2026-07-13"' in prompt_fr
    assert "Donnees athlete" in prompt_fr

    prompt_en = _build_prompt({
        "date": "2026-07-13", "today_session": {}, "language": "en",
    })
    assert "body recomposition" in prompt_en
    assert "Athlete data" in prompt_en
    assert "Tone guidance" in prompt_en and "Consigne de ton" not in prompt_en
    # The tone instruction must never look like a 6th output-line label.
    assert "TONE:" not in prompt_en and "TON :" not in prompt_fr

    # Unknown language falls back to French rather than erroring.
    prompt_fallback = _build_prompt({"language": "de"})
    assert prompt_fallback.startswith(FR_SYSTEM_PROMPT[:20])

    os.environ["LLM_PROVIDER"] = "nonsense"
    try:
        coach({})
        raise AssertionError("expected ValueError for LLM_PROVIDER=nonsense")
    except ValueError:
        pass
    finally:
        del os.environ["LLM_PROVIDER"]

    print("llm.py: all checks passed (no live LLM call made)")
