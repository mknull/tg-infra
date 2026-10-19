#!/usr/bin/env python3
"""Guided configuration; the Linux adapter is loaded only when starting."""

import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    from lib.setup_wizard import run
    raise SystemExit(run())


if __name__ == "__main__":
    main()
