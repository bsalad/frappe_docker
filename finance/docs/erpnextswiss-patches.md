# erpnextswiss patches

We carry our own fixes to erpnextswiss as patches, applied in the image build. Nothing
goes upstream: the fixes stay with us.

**Pin.** The patches apply to erpnextswiss branch `v16` at commit
`5d85c45b48f4774cda6ba84ab75f138ad45a55c3` (`finance/apps.json`, `finance/apps-copy.json`).
A pin change may stop a patch from applying; the build then fails and names the patch.

## Patches

| Patch | Fixes | Why |
|-------|-------|-----|
| `erpnextswiss/0001-item-tools-syntax.patch` | `erpnextswiss/scripts/item_tools.py` line 43: removes a stray `,` after a SQL string. | At the pin the file does not compile (`SyntaxError: invalid syntax`). Importing it fails, and erpnextswiss's test discovery fails with it. The module holds two `@frappe.whitelist()` functions, `get_next_item_code` and `get_voucher_value`, which cannot be called while it does not compile. Nothing we use imports it (see `swiss.md`, Known issues). |

Check a patch by hand against the pin:

```sh
git clone https://github.com/libracore/erpnextswiss && cd erpnextswiss
git checkout 5d85c45
git apply --check ../finance/patches/erpnextswiss/0001-item-tools-syntax.patch
```

## How they are applied

The image build fetches the apps with `bench init` (`images/custom/Containerfile`,
builder stage). Right after that, `finance/scripts/apply-patches.sh` applies each
`finance/patches/<app>/*.patch` in file-name order. Name them with a number prefix so the
order is clear. If a patch does not apply, the build stops with
`patch does not apply to apps/erpnextswiss: <patch>`.

The same step runs for the live image and the copy image, since both use the same app
fetch. Patches are kept in the repository, so a rebuild applies the same fixes.

## Adding a patch

1. Make the change in a checkout of the pinned commit, then take the diff with
   `git diff` from the repository root, so the paths read `erpnextswiss/...`.
2. Save it as `finance/patches/erpnextswiss/NNNN-short-name.patch`, with the next number.
3. Add a row to the table above, with what it fixes and why we carry it.
4. Check it applies (above) and that the patched file compiles.
5. Run the gate (`sh .yardr/check`), which tests the apply step.
