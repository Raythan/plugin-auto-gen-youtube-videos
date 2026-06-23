"""Valida youtube_upload (categoria + publicação + conteúdo alterado) em todos os canais."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.utils.config import load_project_config
from src.youtube.upload_settings import (
    YOUTUBE_CATEGORY_LABELS,
    format_new_channel_youtube_upload_reminder,
    youtube_upload_settings_from_channel,
)


def _check_channels(root: Path, *, remind: str | None) -> int:
    config = load_project_config(root)
    errors: list[str] = []
    ok: list[str] = []

    for channel in config.channels:
        settings = youtube_upload_settings_from_channel(channel.raw)
        if settings is None:
            errors.append(
                f"  - {channel.id}: sem youtube_upload (category_id + contains_synthetic_media)"
            )
            continue
        if not settings.contains_synthetic_media:
            errors.append(
                f"  - {channel.id}: contains_synthetic_media deve ser true "
                "(política YouTube: conteúdo alterado/sintético neste pipeline)"
            )
            continue
        label = YOUTUBE_CATEGORY_LABELS.get(
            settings.category_id, f"id {settings.category_id}"
        )
        publication = (
            "agendado (próxima hora cheia UTC)"
            if settings.publish_mode == "schedule"
            else f"imediato ({settings.privacy_status})"
        )
        ok.append(
            f"  - {channel.id}: {label} (category_id={settings.category_id}), {publication}"
        )

    if remind:
        print(format_new_channel_youtube_upload_reminder(remind))
        return 0

    print("Categorias YouTube por canal (youtube_upload):\n")
    if ok:
        print("OK:")
        print("\n".join(ok))
    if errors:
        print("\nPENDENTE:")
        print("\n".join(errors))
        print(
            "\nIDs comuns: 2 Automóveis | 24 Entretenimento | 28 Ciência e tecnologia"
        )
        print(
            "API alterado/sintético: status.containsSyntheticMedia → "
            "contains_synthetic_media: true no YAML"
        )
        print(
            "\nLembrete para canal novo:\n"
            "  python -m src.youtube.check_upload_config --remind <channel_id>"
        )
        return 1

    if not config.channels:
        print("Nenhum canal carregado em config/channels_config_structure/*.yaml")
        return 1

    print("\nTodos os canais configurados.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verifica youtube_upload (categoria Studio + publicação + conteúdo alterado) por canal."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Raiz do repositório (padrão: auto-detectado)",
    )
    parser.add_argument(
        "--remind",
        metavar="CHANNEL_ID",
        help="Imprime bloco YAML de exemplo para um canal novo",
    )
    args = parser.parse_args(argv)
    return _check_channels(args.root, remind=args.remind)


if __name__ == "__main__":
    sys.exit(main())
