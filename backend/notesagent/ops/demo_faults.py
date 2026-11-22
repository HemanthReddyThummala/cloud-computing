"""Operator CLI for the demo fault switch (ADR-22). Run INSIDE a worker container:

    docker compose exec worker python -m notesagent.ops.demo_faults arm <scenario> --video <VIDEO_ID>
    docker compose exec worker python -m notesagent.ops.demo_faults status
    docker compose exec worker python -m notesagent.ops.demo_faults disarm --video <VIDEO_ID>

Scenarios: dlq | gemini_429_once | frames_partial | frames_all. Arming refuses unless DEMO_FAULTS_ENABLED
is true in this container. An arming expires after 15 minutes; the first job of that video claims it.
Exit codes: 0 ok, 2 refused / invalid input, 3 Redis unavailable.
"""
from __future__ import annotations

import argparse
import re
import sys

import redis

from notesagent.common.config import get_settings
from notesagent.worker.demo_faults import ARM_TTL_SEC, Scenario, video_key

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _client(settings):
    return redis.Redis.from_url(settings.redis_url.get_secret_value(), socket_timeout=5, socket_connect_timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notesagent.ops.demo_faults", description="Demo fault switch (ADR-22)")
    sub = parser.add_subparsers(dest="command", required=True)
    arm = sub.add_parser("arm", help="arm one scenario for the next job of one video (15-minute expiry)")
    arm.add_argument("scenario", choices=[s.value for s in Scenario])
    arm.add_argument("--video", required=True, help="11-character YouTube video ID")
    disarm = sub.add_parser("disarm", help="remove an unclaimed arming")
    disarm.add_argument("--video", required=True)
    sub.add_parser("status", help="list armed videos and bound jobs")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.command == "arm" and not settings.demo_faults_enabled:
        print("REFUSED: DEMO_FAULTS_ENABLED is not true in this worker container; nothing was armed.")
        return 2
    video = getattr(args, "video", None)
    if video is not None and not _VIDEO_ID.fullmatch(video):
        print("REFUSED: --video must be an 11-character YouTube video ID.")
        return 2
    try:
        client = _client(settings)
        if args.command == "arm":
            client.set(video_key(video), args.scenario, ex=ARM_TTL_SEC)
            print(f"ARMED {args.scenario} for the next job of video {video} (expires in {ARM_TTL_SEC // 60} min).")
        elif args.command == "disarm":
            removed = client.delete(video_key(video))
            print(f"DISARMED video {video}." if removed else f"Nothing armed for video {video}.")
        else:
            print(f"demo faults enabled in this container: {settings.demo_faults_enabled}")
            for key in sorted(k.decode() for k in client.scan_iter("demo:fault:*")):
                value = client.get(key)
                ttl = client.ttl(key)
                print(f"{key} = {value.decode() if value else None} (ttl {ttl}s)")
    except redis.RedisError as exc:
        print(f"Redis unavailable ({type(exc).__name__}); nothing changed.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
