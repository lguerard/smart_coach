#!/usr/bin/env python3
"""Private body photos: encrypted storage and a morphology analysis.

A photo in underwear is the most sensitive thing this project will ever
hold, so the design starts from that:

- The picture is re-encoded on upload (EXIF and GPS dropped, orientation
  applied, resized) and stored **encrypted** on disk, never in the
  database and never under a static path. Only its owner can fetch it,
  through an endpoint that checks the session's user id -- admins have
  no special access.
- Nothing about it reaches ntfy, the calendar or the morning message.
- Analysis is opt-in per photo: the image is sent to the configured
  model (Anthropic, through the claude CLI or the API) only when the
  person ticks the box for it, and the page says so.
- The analysis is asked to be factual and kind -- posture, proportions,
  muscle balance, where fat is stored, a wide body-fat range with its
  uncertainty -- with no rating of looks, no diagnosis, and a pointer to
  a professional when something looks like it needs one.

The key comes from ``BODY_PHOTO_KEY`` (a Fernet key) when set; otherwise
one is generated once into ``data/secrets/body_photo.key`` (mode 0600).
Losing the key makes the stored photos unreadable, by design.
"""

import base64
import datetime as dt
import io
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

PHOTO_DIR = Path(os.environ.get(
    "BODY_PHOTO_DIR", Path(__file__).parent / "data" / "body-photos",
))
KEY_FILE = Path(os.environ.get(
    "BODY_PHOTO_KEY_FILE",
    Path(__file__).parent / "data" / "secrets" / "body_photo.key",
))
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_SIDE_PX = 1568  # the size vision models read best; more is just bytes
JPEG_QUALITY = 88
POSES = {"front": "face", "side": "profil", "back": "dos"}
ANALYSIS_MODEL = os.environ.get("BODY_ANALYSIS_MODEL", "claude-opus-5-5")


class PhotoError(ValueError):
    """A photo that cannot be stored or read, with a readable reason."""


# --- encryption -----------------------------------------------------------

def _fernet() -> Fernet:
    """The cipher for stored photos, creating the key file on first use."""
    key = os.environ.get("BODY_PHOTO_KEY")
    if not key:
        if KEY_FILE.exists():
            key = KEY_FILE.read_text().strip()
        else:
            KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode()
            # Created 0600 from the start, never world-readable even
            # for an instant.
            fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(key)
    return Fernet(key.encode() if isinstance(key, str) else key)


# --- image handling -------------------------------------------------------

def clean_image(raw: bytes) -> bytes:
    """Re-encode an upload: orientation applied, metadata gone, resized.

    Re-encoding is what strips EXIF (camera, date, GPS position): the
    new JPEG is written from pixels only.

    Parameters:
        raw (bytes): The uploaded file.

    Returns:
        bytes: A JPEG no larger than ``MAX_SIDE_PX`` on its long side.

    Raises:
        PhotoError: Not an image, or too large.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(raw) > MAX_UPLOAD_BYTES:
        raise PhotoError("Photo trop lourde (15 Mo maximum).")
    try:
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image)
    except (UnidentifiedImageError, OSError) as error:
        raise PhotoError("Ce fichier n'est pas une image lisible.") from error
    image = image.convert("RGB")
    image.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


# --- storage --------------------------------------------------------------

def _path(user_id: int, file_name: str) -> Path:
    return PHOTO_DIR / str(int(user_id)) / file_name


def store_photo(
    conn: sqlite3.Connection, user_id: int, raw: bytes, pose: str,
    local_date: str,
) -> int:
    """Clean, encrypt and save a photo; returns its row id.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owner.
        raw (bytes): The uploaded file.
        pose (str): ``front`` / ``side`` / ``back``.
        local_date (str): ISO local date it was taken (today).

    Returns:
        int: The ``body_photos`` id.

    Raises:
        PhotoError: Bad pose, unreadable or oversized image.
    """
    if pose not in POSES:
        raise PhotoError("Pose inconnue.")
    jpeg = clean_image(raw)
    file_name = f"{secrets.token_hex(16)}.bin"
    path = _path(user_id, file_name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(_fernet().encrypt(jpeg))
    cursor = conn.execute(
        "INSERT INTO body_photos (user_id, created_at, local_date, pose, "
        "file_name) VALUES (?, ?, ?, ?, ?)",
        (user_id, dt.datetime.now(dt.timezone.utc).isoformat(), local_date,
         pose, file_name),
    )
    conn.commit()
    return cursor.lastrowid


def get_photo(
    conn: sqlite3.Connection, user_id: int, photo_id: int,
) -> Optional[sqlite3.Row]:
    """The row for ``photo_id`` -- only if ``user_id`` owns it."""
    return conn.execute(
        "SELECT * FROM body_photos WHERE id = ? AND user_id = ?",
        (photo_id, user_id),
    ).fetchone()


def list_photos(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """The owner's photos, newest first, analyses decoded."""
    rows = conn.execute(
        "SELECT * FROM body_photos WHERE user_id = ? "
        "ORDER BY local_date DESC, id DESC", (user_id,),
    ).fetchall()
    return [
        {**dict(row), "analysis": json.loads(row["analysis"])
         if row["analysis"] else None}
        for row in rows
    ]


def read_photo(
    conn: sqlite3.Connection, user_id: int, photo_id: int,
) -> Optional[bytes]:
    """Decrypted JPEG bytes, or None when not found / not the owner's.

    Raises:
        PhotoError: The file exists but cannot be decrypted (wrong or
            lost key).
    """
    row = get_photo(conn, user_id, photo_id)
    if row is None:
        return None
    path = _path(user_id, row["file_name"])
    if not path.exists():
        return None
    try:
        return _fernet().decrypt(path.read_bytes())
    except InvalidToken as error:
        raise PhotoError(
            "Photo illisible : la cle de chiffrement a change."
        ) from error


def delete_photo(
    conn: sqlite3.Connection, user_id: int, photo_id: int,
) -> bool:
    """Delete one photo, file and row. False when not the owner's."""
    row = get_photo(conn, user_id, photo_id)
    if row is None:
        return False
    _path(user_id, row["file_name"]).unlink(missing_ok=True)
    conn.execute(
        "DELETE FROM body_photos WHERE id = ? AND user_id = ?",
        (photo_id, user_id),
    )
    conn.commit()
    return True


def delete_all(conn: sqlite3.Connection, user_id: int) -> int:
    """Delete every photo of ``user_id``; returns how many."""
    ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM body_photos WHERE user_id = ?", (user_id,),
        )
    ]
    for photo_id in ids:
        delete_photo(conn, user_id, photo_id)
    folder = PHOTO_DIR / str(int(user_id))
    if folder.exists() and not any(folder.iterdir()):
        folder.rmdir()
    return len(ids)


# --- analysis -------------------------------------------------------------

def _string_list(description: str) -> dict:
    return {"type": "array", "items": {"type": "string"},
            "description": description}


ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "usable", "unusable_reason", "summary", "photo_tips", "posture",
        "proportions", "muscle_balance", "fat_distribution",
        "body_fat_estimate", "symmetry", "strengths", "priorities",
        "training_advice", "posture_exercises", "nutrition_advice",
        "progress_vs_previous", "see_professional",
    ],
    "properties": {
        "usable": {"type": "boolean"},
        "unusable_reason": {"type": "string"},
        "summary": {"type": "string"},
        "photo_tips": _string_list(
            "How to take the next photo so it compares well"),
        "posture": {
            "type": "object", "additionalProperties": False,
            "required": ["summary", "observations"],
            "properties": {
                "summary": {"type": "string"},
                "observations": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["area", "finding", "degree"],
                    "properties": {
                        "area": {"type": "string"},
                        "finding": {"type": "string"},
                        "degree": {"type": "string",
                                   "enum": ["aucun", "leger", "marque"]},
                    },
                }},
            },
        },
        "proportions": {"type": "string"},
        "muscle_balance": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["zone", "level", "note"],
            "properties": {
                "zone": {"type": "string"},
                "level": {"type": "string", "enum": [
                    "a developper", "moyen", "developpe", "non visible"]},
                "note": {"type": "string"},
            },
        }},
        "fat_distribution": {"type": "string"},
        "body_fat_estimate": {
            "type": "object", "additionalProperties": False,
            "required": ["low_pct", "high_pct", "confidence", "note"],
            "properties": {
                "low_pct": {"type": "number"},
                "high_pct": {"type": "number"},
                "confidence": {"type": "string",
                               "enum": ["faible", "moyenne", "bonne"]},
                "note": {"type": "string"},
            },
        },
        "symmetry": {"type": "string"},
        "strengths": _string_list("What is going well, concretely"),
        "priorities": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "why", "how"],
            "properties": {
                "title": {"type": "string"},
                "why": {"type": "string"},
                "how": {"type": "string"},
            },
        }},
        "training_advice": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["session_type", "advice"],
            "properties": {
                "session_type": {"type": "string"},
                "advice": {"type": "string"},
            },
        }},
        "posture_exercises": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "how", "when"],
            "properties": {
                "name": {"type": "string"},
                "how": {"type": "string"},
                "when": {"type": "string"},
            },
        }},
        "nutrition_advice": _string_list(
            "Consistent with the app's targets given in the context"),
        "progress_vs_previous": {"type": "string"},
        "see_professional": {"type": "string"},
    },
}

ANALYSIS_PROMPT_FR = """\
Tu es un coach sportif diplome qui fait un bilan morphologique a partir \
d'une photo que la personne a prise d'elle-meme, en sous-vetements, pour \
suivre sa recomposition corporelle (perdre du gras, prendre du muscle). \
Elle te la confie dans un espace prive ; reponds avec le serieux et la \
bienveillance d'un bon professionnel.

D'abord, verifie que la photo s'y prete. usable=false (et unusable_reason \
explique pourquoi, gentiment) si : ce n'est pas une personne, la personne \
semble mineure, il y a plusieurs personnes, la personne est nue au-dela \
des sous-vetements, ou l'image est trop floue/sombre/coupee pour juger. \
Dans ce cas, remplis les autres champs avec des chaines et listes vides \
et des zeros.

Sinon, analyse UNIQUEMENT ce que la photo permet de voir, pose : {pose}.
- posture : epaules (enroulees, hauteur), tete en avant, courbure du dos, \
bascule du bassin, genoux, appuis. degree = aucun / leger / marque.
- proportions : carrure, taille, rapport epaules-taille, longueur relative \
des segments, en termes neutres.
- muscle_balance : par zone visible (epaules, pectoraux, bras, dos, \
abdominaux, fessiers, cuisses, mollets) ; "non visible" si la pose ne \
permet pas de juger.
- fat_distribution : ou le gras se stocke surtout, sans jugement.
- body_fat_estimate : une FOURCHETTE large (au moins 4 points d'ecart), \
une confiance honnete, et rappelle dans note qu'une estimation visuelle \
est imprecise. Si la balance donne un taux (contexte), confronte-le.
- symmetry : desequilibres gauche/droite visibles, s'il y en a.
- strengths : ce qui va bien, concretement.
- priorities : 3 au plus, les plus utiles pour SON objectif, chacune avec \
pourquoi et comment.
- training_advice : relie aux seances qu'il fait deja (types fournis dans \
le contexte : treadmill, lower_body, upper_body, calisthenics) -- quoi \
accentuer ou ajouter, sans materiel supplementaire.
- posture_exercises : 2 a 4 exercices correctifs simples, dont au moins \
un faisable discretement au bureau ; name, how, when.
- nutrition_advice : coherent avec les cibles du contexte (proteines, \
calories, rythme vise) ; jamais de regime extreme.
- progress_vs_previous : compare a l'analyse precedente fournie (meme \
pose) si elle existe, sinon chaine vide.
- photo_tips : comment refaire la photo pour comparer (meme lumiere, \
distance, heure, pose).
- see_professional : chaine vide sauf si quelque chose merite un avis \
medical ou de kine (asymetrie marquee du dos, lesion de peau visible, \
maigreur ou prise de poids inquietante...) ; dis-le calmement, sans \
diagnostic.
- summary : 2 ou 3 phrases chaleureuses et honnetes.

Regles : en francais, tutoiement. Factuel, precis, bienveillant. Aucune \
note de beaute ni d'attractivite, aucun commentaire sur les parties \
intimes, aucune comparaison a un ideal, aucun diagnostic medical. Si \
le taux de gras parait deja bas, ne recommande pas de secher davantage. \
N'invente rien que la photo ne montre pas.

Contexte (donnees de l'application) :
{context}

Analyse precedente de la meme pose :
{previous}
"""


def build_context(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """What the app knows that makes the analysis personal.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owner.
        date (str): ISO local date.

    Returns:
        dict: Measurements, goal, targets and the session types/levels
        the advice should tie into. Only keys with data.
    """
    import db
    import metrics
    import progress
    import training

    context: dict = {**metrics.latest_body_comp(conn, user_id, date)}
    for key in ("height_cm", "age_years", "sex", "target_weight_kg",
                "weekly_weight_change_kg"):
        value = db.get_setting(conn, user_id, key)
        if value:
            context[key] = value
    targets = progress.macro_targets(conn, user_id, date)
    for key in ("calorie_target_kcal", "protein_target_g"):
        if targets.get(key):
            context[key] = targets[key]
    context["session_levels"] = {
        session_type: training.get_level(conn, user_id, session_type)
        for session_type in training.SESSION_LABEL_FR
    }
    context["goal"] = "recomposition corporelle"
    return context


def _previous_analysis(
    conn: sqlite3.Connection, user_id: int, photo: sqlite3.Row,
) -> Optional[dict]:
    """The last usable analysis of the same pose, before this photo."""
    row = conn.execute(
        "SELECT local_date, analysis FROM body_photos WHERE user_id = ? "
        "AND pose = ? AND id != ? AND analysis IS NOT NULL AND "
        "(local_date < ? OR (local_date = ? AND id < ?)) "
        "ORDER BY local_date DESC, id DESC LIMIT 1",
        (user_id, photo["pose"], photo["id"], photo["local_date"],
         photo["local_date"], photo["id"]),
    ).fetchone()
    if not row:
        return None
    analysis = json.loads(row["analysis"])
    if not analysis.get("usable"):
        return None
    keep = ("summary", "posture", "muscle_balance", "body_fat_estimate",
            "fat_distribution", "symmetry")
    return {"date": row["local_date"],
            **{key: analysis.get(key) for key in keep}}


def _prompt(pose: str, context: dict, previous: Optional[dict]) -> str:
    return ANALYSIS_PROMPT_FR.format(
        pose=POSES.get(pose, pose),
        context=json.dumps(context, ensure_ascii=False),
        previous=json.dumps(previous, ensure_ascii=False)
        if previous else "aucune",
    )


def _analyze_claude_cli(jpeg: bytes, prompt: str) -> dict:
    """Ask Claude through the CLI: image in stream-json, JSON out.

    No tools at all (``--tools ""``): the model only looks at the image
    it is given, it cannot read files or run anything.
    """
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    message = {"type": "user", "message": {"role": "user", "content": [
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg).decode(),
        }},
        {"type": "text", "text": prompt},
    ]}}
    result = subprocess.run(
        [claude, "-p", "--input-format", "stream-json",
         "--output-format", "stream-json", "--verbose", "--tools", "",
         "--model", ANALYSIS_MODEL,
         "--json-schema", json.dumps(ANALYSIS_SCHEMA)],
        input=json.dumps(message) + "\n", capture_output=True, text=True,
        timeout=600,
    )
    for line in reversed(result.stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "result":
            continue
        if event.get("is_error") or event.get("subtype") != "success":
            raise RuntimeError(
                f"analyse refusee ou echouee: {event.get('result') or event}"
            )
        if event.get("structured_output") is not None:
            return event["structured_output"]
        return json.loads(event["result"])
    raise RuntimeError(f"claude CLI sans resultat: {result.stderr[-500:]}")


def _analyze_anthropic_api(jpeg: bytes, prompt: str) -> dict:
    """Ask Claude through the API, schema-constrained JSON out.

    Server-side refusal fallbacks are on, so a safety decline on the main
    model is retried on another one inside the same call.
    """
    import anthropic  # optional dependency, as in llm.py

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=ANALYSIS_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA},
        },
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.standard_b64encode(jpeg).decode(),
            }},
            {"type": "text", "text": prompt},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("le modele a refuse d'analyser cette photo")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("analyse tronquee (max_tokens)")
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


PROVIDERS = {
    "claude_cli": _analyze_claude_cli,
    "anthropic_api": _analyze_anthropic_api,
}


def analyze_photo(
    conn: sqlite3.Connection, user_id: int, photo_id: int,
) -> dict:
    """Run the morphology analysis on one photo and store it.

    Parameters:
        conn (sqlite3.Connection): smart_coach db connection.
        user_id (int): Owner (checked).
        photo_id (int): The photo.

    Returns:
        dict: The stored analysis.

    Raises:
        PhotoError: Not found / not the owner's / unreadable.
        RuntimeError: The model call failed (also stored on the row, so
            the page can say what happened).
    """
    photo = get_photo(conn, user_id, photo_id)
    jpeg = read_photo(conn, user_id, photo_id)
    if photo is None or jpeg is None:
        raise PhotoError("Photo introuvable.")
    prompt = _prompt(
        photo["pose"], build_context(conn, user_id, photo["local_date"]),
        _previous_analysis(conn, user_id, photo),
    )
    provider = os.environ.get("LLM_PROVIDER", "claude_cli")
    if provider not in PROVIDERS:
        raise RuntimeError(f"LLM_PROVIDER inconnu: {provider}")
    try:
        analysis = PROVIDERS[provider](jpeg, prompt)
    except Exception as error:
        conn.execute(
            "UPDATE body_photos SET analysis_error = ? WHERE id = ? AND "
            "user_id = ?", (str(error)[:500], photo_id, user_id),
        )
        conn.commit()
        raise
    conn.execute(
        "UPDATE body_photos SET analysis = ?, analysis_error = NULL, "
        "analyzed_at = ?, model = ? WHERE id = ? AND user_id = ?",
        (json.dumps(analysis, ensure_ascii=False),
         dt.datetime.now(dt.timezone.utc).isoformat(), ANALYSIS_MODEL,
         photo_id, user_id),
    )
    conn.commit()
    return analysis


if __name__ == "__main__":
    import tempfile

    import db

    from PIL import Image

    tmp = Path(tempfile.mkdtemp())
    PHOTO_DIR = tmp / "photos"
    KEY_FILE = tmp / "secrets" / "body_photo.key"
    os.environ.pop("BODY_PHOTO_KEY", None)

    conn = db.connect(tmp / "body.db")
    db.init_db(conn)
    owner = db.create_user(conn, "owner", "password1234")
    other = db.create_user(conn, "other", "password1234")

    # An upload carrying EXIF (here a GPS-like comment and an
    # orientation flag) comes out with none of it, rotated upright.
    source = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90 degrees
    exif[0x010E] = "lat 48.85 lon 2.35"  # ImageDescription
    buf = io.BytesIO()
    source.save(buf, "JPEG", exif=exif)
    cleaned = clean_image(buf.getvalue())
    reopened = Image.open(io.BytesIO(cleaned))
    assert not reopened.getexif(), "metadata must be stripped"
    assert reopened.size == (200, 400), reopened.size  # orientation applied
    # Large uploads are resized; non-images and oversize files refused.
    big = io.BytesIO()
    Image.new("RGB", (4000, 3000), "white").save(big, "JPEG")
    assert max(Image.open(io.BytesIO(clean_image(big.getvalue()))).size) == (
        MAX_SIDE_PX
    )
    for bad in (b"not an image", b"x" * (MAX_UPLOAD_BYTES + 1)):
        try:
            clean_image(bad)
            raise AssertionError("expected PhotoError")
        except PhotoError:
            pass

    # Stored encrypted: the file on disk is not a JPEG, the key file is
    # private, and only the owner can read, list or delete the photo.
    photo_id = store_photo(conn, owner, buf.getvalue(), "front", "2026-10-04")
    row = get_photo(conn, owner, photo_id)
    on_disk = (PHOTO_DIR / str(owner) / row["file_name"]).read_bytes()
    assert not on_disk.startswith(b"\xff\xd8"), "must not be a plain JPEG"
    assert oct(KEY_FILE.stat().st_mode & 0o777) == "0o600"
    assert oct((PHOTO_DIR / str(owner) / row["file_name"]).stat().st_mode
               & 0o777) == "0o600"
    assert read_photo(conn, owner, photo_id).startswith(b"\xff\xd8")
    assert read_photo(conn, other, photo_id) is None
    assert get_photo(conn, other, photo_id) is None
    assert list_photos(conn, other) == []
    assert not delete_photo(conn, other, photo_id)
    assert read_photo(conn, owner, photo_id) is not None  # still there
    # Nothing of the picture is in the database.
    assert all(
        b"\xff\xd8" not in bytes(str(value), "latin-1", "ignore")
        for value in dict(row).values()
    )
    try:
        store_photo(conn, owner, buf.getvalue(), "selfie", "2026-10-04")
        raise AssertionError("expected PhotoError for an unknown pose")
    except PhotoError:
        pass

    # A changed key is reported as such, not as a crash.
    os.environ["BODY_PHOTO_KEY"] = Fernet.generate_key().decode()
    try:
        read_photo(conn, owner, photo_id)
        raise AssertionError("expected PhotoError with the wrong key")
    except PhotoError as error:
        assert "cle" in str(error)
    del os.environ["BODY_PHOTO_KEY"]

    # Analysis: the provider gets the decrypted image and a prompt with
    # the person's context and the previous same-pose analysis; the
    # result is stored; a failure is stored too and re-raised.
    seen = {}

    def _fake(jpeg, prompt):
        seen["jpeg"], seen["prompt"] = jpeg, prompt
        return {"usable": True, "summary": "ok", "posture": {},
                "muscle_balance": [], "body_fat_estimate": {},
                "fat_distribution": "", "symmetry": ""}

    PROVIDERS["fake"] = _fake
    os.environ["LLM_PROVIDER"] = "fake"
    db.set_setting(conn, owner, "height_cm", "180")
    first = analyze_photo(conn, owner, photo_id)
    assert first["summary"] == "ok"
    assert seen["jpeg"].startswith(b"\xff\xd8")
    assert '"height_cm": "180"' in seen["prompt"], seen["prompt"]
    assert "Analyse precedente de la meme pose :\naucune" in seen["prompt"]
    assert list_photos(conn, owner)[0]["analysis"]["summary"] == "ok"
    second_id = store_photo(conn, owner, buf.getvalue(), "front", "2026-10-11")
    analyze_photo(conn, owner, second_id)
    assert '"date": "2026-10-04"' in seen["prompt"], "previous not passed"
    # Another pose has no previous analysis.
    side_id = store_photo(conn, owner, buf.getvalue(), "side", "2026-10-11")
    analyze_photo(conn, owner, side_id)
    assert "meme pose :\naucune" in seen["prompt"]
    try:
        analyze_photo(conn, other, photo_id)
        raise AssertionError("another user must not analyze it")
    except PhotoError:
        pass

    def _broken(jpeg, prompt):
        raise RuntimeError("panne")

    PROVIDERS["fake"] = _broken
    try:
        analyze_photo(conn, owner, side_id)
        raise AssertionError("expected the failure to propagate")
    except RuntimeError:
        pass
    assert get_photo(conn, owner, side_id)["analysis_error"] == "panne"
    # A failed re-run keeps the earlier good analysis.
    assert get_photo(conn, owner, side_id)["analysis"] is not None
    del os.environ["LLM_PROVIDER"]

    # Schema: every property is required and closed, as structured
    # outputs need.
    def _closed(node):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node
            assert set(node["required"]) == set(node["properties"]), node
            for child in node["properties"].values():
                _closed(child)
        if node.get("type") == "array":
            _closed(node["items"])

    _closed(ANALYSIS_SCHEMA)

    # Delete all: files and rows gone, the other user's untouched.
    other_id = store_photo(conn, other, buf.getvalue(), "back", "2026-10-04")
    assert delete_all(conn, owner) == 3
    assert list_photos(conn, owner) == []
    assert not (PHOTO_DIR / str(owner)).exists()
    assert read_photo(conn, other, other_id) is not None

    print("body.py: all checks passed (no live model call made)")
