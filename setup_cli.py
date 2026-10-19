#!/usr/bin/env python3
"""Guided configuration with prerequisite checks before paid generation."""

import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    from lib.setup_wizard import run
    raise SystemExit(run())


if __name__ == "__main__":
    main()
