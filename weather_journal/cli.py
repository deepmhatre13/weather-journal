"""Command Line Interface for Weather Journal."""

import argparse
import sys
from weather_journal.fetch import get_current
from weather_journal.store import (
    AmbiguousLocationError,
    save_entry,
    get_history,
    DEFAULT_DB_PATH,
)
from weather_journal.stats import avg_temp, hottest_day, rainy_days

# Exit code used for expected operational failures (bad input, API errors, ...).
EXIT_FAILURE = 1


def cmd_add(args):
    save_entry(args.city, args.temp, args.humidity, args.condition, db_path=args.db)
    print(f"Logged manually: {args.city} - {args.temp}°C, {args.humidity}% humidity, {args.condition}")


def cmd_log(args):
    try:
        data = get_current(args.city, args.key)
    except ValueError as e:
        print(f"Error fetching weather: {e}", file=sys.stderr)
        return EXIT_FAILURE

    save_entry(
        data["city"], data["temp"], data["humidity"], data["condition"],
        db_path=args.db, location=data["location"],
    )
    print(f"Logged: {data['city']} - {data['temp']}°C, {data['humidity']}% humidity, {data['condition']}")
    return 0


def cmd_history(args):
    try:
        records = get_history(args.city, db_path=args.db)
    except AmbiguousLocationError as error:
        print(error, file=sys.stderr)
        return EXIT_FAILURE
    if not records:
        print("No records found.")
        return 0

    title = f"Weather History for {args.city}" if args.city else "All Weather History"
    print(f"\n{title}:")
    print("-" * 75)
    print(f"{'City':<16} {'Temp':<10} {'Humidity':<12} {'Condition':<20} {'Logged At':<18}")
    print("-" * 75)
    for r in records:
        print(f"{r['location_label']:<16} {r['temp']}°C{'':<6} {r['humidity']}%{'':<8} {r['condition']:<20} {r['logged_at']}")

    print()
    return 0


def cmd_stats(args):
    try:
        records = get_history(args.city, db_path=args.db)
    except AmbiguousLocationError as error:
        print(error, file=sys.stderr)
        return EXIT_FAILURE
    if not records:
        print(f"No records logged for '{args.city}'.")
        return 0

    avg = avg_temp(records)
    hot = hottest_day(records)
    rain = rainy_days(records)

    print(f"\nWeather Statistics for {args.city}:")
    print("-" * 45)
    print(f"  • Total Observations: {len(records)}")
    print(f"  • Average Temperature: {avg:.1f}°C")
    if hot:
        print(f"  • Hottest Day: {hot['temp']}°C on {hot['logged_at']}")
    print(f"  • Rainy Observations: {rain}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Weather Journal CLI")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to sqlite database")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # log
    p_log = subparsers.add_parser("log", help="Log current weather for a city")
    p_log.add_argument("city", help="City name (e.g. Mumbai, London)")
    p_log.add_argument("--key", default=None, help="OpenWeatherMap API Key")
    p_log.set_defaults(func=cmd_log)

    # add
    p_add = subparsers.add_parser("add", help="Log weather by hand")
    p_add.add_argument("city", help="City name")
    p_add.add_argument("temp", type=float, help="Temperature in °C")
    p_add.add_argument("humidity", type=int, help="Humidity percentage")
    p_add.add_argument("condition", help="Weather condition description")
    p_add.set_defaults(func=cmd_add)

    # history
    p_hist = subparsers.add_parser("history", help="Show logged weather history")
    p_hist.add_argument("city", nargs="?", default=None, help="City name")
    p_hist.set_defaults(func=cmd_history)

    # stats
    p_stats = subparsers.add_parser("stats", help="Compute statistics for a city")
    p_stats.add_argument("city", help="City name")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    # Command handlers return EXIT_FAILURE on expected operational failures so
    # the exit code is nonzero; anything else propagates so bugs still crash
    # loudly and are not swallowed by the CLI layer.
    status = args.func(args)
    return 0 if status is None else status


if __name__ == "__main__":
    sys.exit(main())
