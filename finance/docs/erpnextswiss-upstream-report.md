<!-- DRAFT, NOT POSTED. Benchi decides whether and under which account this is posted. -->

# Upstream bug report: `scripts/item_tools.py` has a SyntaxError on line 43 (v16 at 5d85c45)

**Target:** https://github.com/libracore/erpnextswiss, branch `v16`

## Summary

On branch `v16` at commit `5d85c45b48f4774cda6ba84ab75f138ad45a55c3`, the file
`erpnextswiss/scripts/item_tools.py` does not compile. Line 43 has a stray comma
after a SQL string, so importing the module raises `SyntaxError`. Because of it,
test discovery fails, and the test `test_item_cleanup_native` cannot import its module.

## Versions

- erpnextswiss 1.34.1 (the `Version` in the package metadata), commit `5d85c45`, branch `v16`
- Frappe 16.50.0, ERPNext 16.50.0

## Location

`erpnextswiss/scripts/item_tools.py`, line 43. The lines around it:

```python
            {
                'voucher': voucher_code,
                'customer': customer
```

Line 42 ends the SQL string with `""",` and line 43 is a bare `,` on its own line.
Deleting line 43, or the comma at the end of line 42, makes the file compile (checked on a copy).

## Reproduce

```sh
python3 -m py_compile erpnextswiss/scripts/item_tools.py
```

Output on `5d85c45`:

```text
  File "erpnextswiss/scripts/item_tools.py", line 43
    ,
    ^
SyntaxError: invalid syntax
```

Exit status 1.

## Impact

- `import erpnextswiss.scripts.item_tools` fails.
- Test discovery for the app fails on this module, and
  `test_item_cleanup_native` errors on import.
- Within the package, only the test module and a verifier script refer to it. Hooks,
  whitelisted methods and pages do not import it, so the runtime is not affected.

## Request

Remove the stray comma on line 43, and add a CI step that byte-compiles the
package, so a file like this fails before it reaches `v16`.
