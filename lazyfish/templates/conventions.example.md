# Project conventions

Copy this file to `.lazyfish/conventions.md` in your repository and replace
every line with something true about *your* project. lazyfish injects the file
verbatim into the context it prepares for each ticket, and never edits it.

What belongs here is the knowledge a competent stranger would not guess from
reading the code for ten minutes. What does not belong here is anything a
linter, formatter or type checker already enforces: those tools report their own
rules better than prose can.

## Layout

- `src/` holds the application; `tests/` mirrors its structure one-for-one.
- Anything under `src/legacy/` is frozen. Changes there need a second reviewer.
- Database migrations live in `migrations/` and are append-only.

## Testing

- Run the suite with `<your command here>`.
- A change to `<important module>` needs an integration test, not only a unit test.
- Tests must not reach the network.

## Conventions a newcomer gets wrong

- Times are stored in UTC and converted only at the presentation layer.
- Money is integer minor units. There is no float anywhere near a price.
- Public API responses are versioned; adding a required field is a breaking change.

## Do not touch without asking

- `src/billing/` - changes need sign-off from the payments owner.
- Anything that writes to the `audit_log` table.
