"""Verifica status OAuth do YouTube por canal (sem abrir browser)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.utils.config import load_dotenv_files, load_project_config
from src.youtube.oauth_status import OAuthHealth, check_all_channels

_HEALTH_LABELS: dict[OAuthHealth, str] = {
    OAuthHealth.OK: "OK",
    OAuthHealth.OK_REFRESHED: "OK (refresh)",
    OAuthHealth.EXPIRING_SOON: "ATENÇÃO (expira em breve)",
    OAuthHealth.MISSING_TOKEN: "SEM TOKEN",
    OAuthHealth.MISSING_CLIENT: "SEM CLIENT SECRET",
    OAuthHealth.INVALID_TOKEN: "TOKEN INVÁLIDO",
    OAuthHealth.NEEDS_REAUTH: "REAUTORIZAR",
}


def _format_report_line(report) -> str:
    label = _HEALTH_LABELS.get(report.health, report.health.value)
    expiry = ""
    if report.expiry_utc:
        expiry = f" | expira {report.expiry_utc.strftime('%Y-%m-%d %H:%M UTC')}"
    return (
        f"  - {report.channel_id} (conta {report.youtube_account}): {label}{expiry}\n"
        f"    token: {report.token_path}\n"
        f"    {report.message}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verifica tokens OAuth do YouTube por canal habilitado."
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Checar apenas este channel id (default: todos os habilitados).",
    )
    parser.add_argument(
        "--warn-days",
        type=int,
        default=7,
        help="Dias antes da expiração do access token para marcar ATENÇÃO (default: 7).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Saída JSON (uma linha por canal).",
    )
    args = parser.parse_args(argv if argv is not None else None)

    root = Path(__file__).resolve().parents[2]
    load_dotenv_files(root)
    config = load_project_config(root)

    channels = config.enabled_channels
    if args.channel:
        channels = [config.channel(args.channel)]

    reports = check_all_channels(
        root,
        config,
        channels=channels,
        expiry_warning_days=max(1, args.warn_days),
    )

    if args.json:
        import json

        payload = [
            {
                "channel_id": r.channel_id,
                "youtube_account": r.youtube_account,
                "token_path": r.token_path,
                "health": r.health.value,
                "message": r.message,
                "expiry_utc": r.expiry_utc.isoformat() if r.expiry_utc else None,
                "needs_attention": r.needs_attention,
            }
            for r in reports
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("Status OAuth YouTube por canal:\n")
        if not reports:
            print("Nenhum canal habilitado em config/channels_config_structure/.")
            return 1
        for report in reports:
            print(_format_report_line(report))
            print()

        attention = [r for r in reports if r.needs_attention]
        hints = [r for r in reports if r.is_informational_warning]
        if attention:
            print(
                f"{len(attention)} canal(is) precisam de reautorização. "
                "Reautorize com:\n"
                "  python -m src.pipeline oauth-reauth --channel <channel_id>\n"
                "(Na VM com SSH -L para abrir o browser localmente; ver docs/deploy-oci-vm.md)"
            )
        elif hints:
            print(
                f"{len(hints)} canal(is) com access token perto de expirar "
                "(refresh automático costuma resolver; reauth só se o upload falhar)."
            )
        else:
            print("Todos os canais verificados estão OK para upload.")

    bad = [r for r in reports if r.health in {
        OAuthHealth.MISSING_TOKEN,
        OAuthHealth.MISSING_CLIENT,
        OAuthHealth.INVALID_TOKEN,
        OAuthHealth.NEEDS_REAUTH,
    }]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
