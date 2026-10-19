"""Explicit cache-only migration: python backend/disclosure_cache_migrate.py FILE."""
import argparse
import json

import disclosure_cache


def main():
    parser = argparse.ArgumentParser(description="Import disclosure JSON into an indexed sidecar; retain source")
    parser.add_argument("path")
    args = parser.parse_args()
    print(json.dumps(disclosure_cache.migrate(args.path)))


if __name__ == "__main__":
    main()
