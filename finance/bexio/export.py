"""Export the bexio master data to private JSON files, one per entity.

Step one of the master-data pipeline; import_master.py reads the files and
puts them into ERPNext. GET only (client.py has no way to write). Nothing is
masked: the files are the private copy of the account's data, so they go to
/Users/bsaladin/ws_yardr_finance/private/ (directory mode 700, files 600) and
never into the repository.

Run it as:
    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/export.py [--out DIR]

--out defaults to <private>/bexio-export/<YYYY-MM-DD>/. Besides the entity
files the directory gets manifest.json: per entity the file, the record count
and the status. An entity the token has no scope for (403) or the API does not
know (404) is recorded in the manifest and skipped, so one gap does not stop
the run; the exit status is 2 when a required entity is missing.

Invoices and bills are exported for their parties only: they decide whether a
contact becomes a Customer, a Supplier or both. The documents themselves are
imported by a later bead.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import BexioError, Client  # noqa: E402

PRIVATE = "/Users/bsaladin/ws_yardr_finance/private"


def offset(path):
    return ("offset", path, None)


def pages(path, size_param, rows_key):
    return ("pages", path, (size_param, rows_key))


# file name -> (how to read it, required). Required entities are what the
# import cannot do without; the lookups only translate ids into names.
ENTITIES = {
    "contacts": (offset("/2.0/contact"), True),
    "contact_groups": (offset("/2.0/contact_group"), True),
    "contact_relations": (offset("/2.0/contact_relation"), True),
    "articles": (offset("/2.0/article"), True),
    "accounts": (offset("/2.0/accounts"), True),
    "account_groups": (offset("/2.0/account_groups"), False),
    "currencies": (offset("/3.0/currencies"), True),
    "bank_accounts": (offset("/3.0/banking/accounts"), True),
    "invoices": (offset("/2.0/kb_invoice"), True),
    "bills": (pages("/4.0/purchase/bills", "page_size", "data"), True),
    "countries": (offset("/2.0/country"), False),
    "units": (offset("/2.0/unit"), False),
    "salutations": (offset("/2.0/salutation"), False),
    "languages": (offset("/2.0/language"), False),
}


def default_out(today=None):
    today = today or datetime.date.today()
    return os.path.join(PRIVATE, "bexio-export", today.isoformat())


def read_entity(client, spec):
    kind, path, extra = spec
    if kind == "offset":
        return list(client.paginate(path))
    size_param, rows_key = extra
    return list(client.paginate_pages(path, size_param, rows_key))


def write_private(path, data):
    """Write JSON with mode 600, whatever the umask."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def export(client, out, entities=None):
    """Write every entity to `out` and return the manifest (also written there)."""
    entities = entities or ENTITIES
    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)
    manifest = {"exported_on": datetime.date.today().isoformat(), "entities": {}}
    for name, (spec, required) in entities.items():
        entry = {"file": name + ".json", "required": required}
        try:
            rows = read_entity(client, spec)
        except BexioError as err:
            # A stale file from an earlier run must not pass for this run's data.
            stale = os.path.join(out, entry["file"])
            if os.path.exists(stale):
                os.remove(stale)
            entry.update(status="error", error="HTTP {} on {}".format(err.status, err.path), count=0)
        else:
            write_private(os.path.join(out, entry["file"]), rows)
            entry.update(status="ok", count=len(rows))
        manifest["entities"][name] = entry
    write_private(os.path.join(out, "manifest.json"), manifest)
    return manifest


def failed_required(manifest):
    return [n for n, e in manifest["entities"].items() if e["required"] and e["status"] != "ok"]


def main(argv):
    parser = argparse.ArgumentParser(description="Export bexio master data to private JSON files (read-only).")
    parser.add_argument("--out", default=None, help="target directory (default: <private>/bexio-export/<today>/)")
    args = parser.parse_args(argv)
    out = args.out or default_out()
    manifest = export(Client(), out)
    print("exported to {}".format(out))
    for name, entry in manifest["entities"].items():
        print("  {:<18} {}".format(name, entry["count"] if entry["status"] == "ok" else entry["error"]))
    missing = failed_required(manifest)
    if missing:
        print("missing required entities: {}".format(", ".join(missing)), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
