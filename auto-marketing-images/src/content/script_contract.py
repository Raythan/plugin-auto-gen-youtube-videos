"""Validação do contrato JSON que o Gemini deve devolver.

Regras codificadas aqui espelham as regras passadas no prompt. Uma falha de
validação levanta ``ValueError`` com mensagem em pt-BR para que o chamador
possa registrar em log e acionar o fallback.
"""
from __future__ import annotations

import math
import re

from src.models import ScriptResult


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,59}$")


def validate_script_result(
    result: ScriptResult,
    script_target: dict[str, int] | None = None,
    *,
    require_topic_key: bool = False,
    require_visual_scenes: bool = True,
) -> None:
    """Verifica se ``result`` satisfaz o contrato mínimo do pipeline.

    Levanta ``ValueError`` (mensagem pt-BR) na primeira violação encontrada.
    """
    target = script_target or {}

    # --- script_text ---
    if not result.script_text or not result.script_text.strip():
        raise ValueError("script_text esta vazio; o Gemini nao gerou roteiro.")

    min_words: int = int(target.get("min_words", 0))
    max_words: int = int(target.get("max_words", 9999))
    word_count = len(result.script_text.split())
    if min_words and word_count < min_words:
        raise ValueError(
            f"script_text tem {word_count} palavras, minimo esperado e {min_words}."
        )
    if word_count > max_words * 1.5:
        raise ValueError(
            f"script_text tem {word_count} palavras, muito acima do maximo de {max_words}."
        )

    # --- tags ---
    if not result.tags or len(result.tags) < 3:
        raise ValueError(
            f"Campo 'tags' tem {len(result.tags)} entradas; minimo e 3 (ideal 5–10)."
        )

    # --- visual_scenes ---
    if require_visual_scenes:
        if not result.visual_scenes:
            raise ValueError("Campo 'visual_scenes' esta vazio; necessario para ai_slides.")
        max_secs: int = int(target.get("max_seconds", 35))
        min_scenes = max(7, math.ceil(max_secs / 3))
        if len(result.visual_scenes) < max(3, min_scenes - 2):
            raise ValueError(
                f"visual_scenes tem {len(result.visual_scenes)} cenas; "
                f"esperado pelo menos {min_scenes - 2} para audio de {max_secs}s."
            )
        empty_prompts = [i for i, s in enumerate(result.visual_scenes) if not s.prompt_en.strip()]
        if empty_prompts:
            raise ValueError(
                f"visual_scenes com prompt_en vazio nas posicoes: {empty_prompts}."
            )

    # --- topic_key (opcional, ativado para freeform) ---
    if require_topic_key:
        if not result.topic_key:
            raise ValueError("Campo 'topic_key' ausente; necessario para anti-repeticao no modo gemini_freeform.")
        if not _SLUG_RE.match(result.topic_key):
            raise ValueError(
                f"topic_key '{result.topic_key}' invalido; use kebab-case com letras, numeros e hifens."
            )
