"""The decisions of switch-image.sh, without docker or the network: which tag the stack runs,
what finance-local.yml says after a switch, and which images to remove so that one
frappe-finance-custom image is at rest (finance/docs/erpnext-setup.md).

usage: switch_image.py current <compose file>
       switch_image.py set <compose file> <tag>
       switch_image.py remove <previous tag> <new tag>   (prints the image refs, one per line)
"""

import re
import sys

REPO = "frappe-finance-custom"
IMAGE = re.compile(r"^(\s*image:\s*)" + re.escape(REPO) + r":(\S+)\s*$", re.M)


def current_tag(compose_text):
    """The one tag every service in the compose file runs. Refuses a mix: a switch
    rewrites all of them, so a mixed file would not come back to a known state."""
    tags = {m.group(2) for m in IMAGE.finditer(compose_text)}
    if len(tags) != 1:
        raise ValueError(f"expected one {REPO} tag in the compose file, found {sorted(tags) or 'none'}")
    return tags.pop()


def set_tag(compose_text, tag):
    """The same compose file with every service on the tag. Other lines stay as they are."""
    return IMAGE.sub(lambda m: f"{m.group(1)}{REPO}:{tag}", compose_text)


def remove_after_switch(previous, new):
    """The image refs a good switch replaces: the previous image and its -base layer, and the
    new image's -base (the build's intermediate, which must not stay tagged at rest)."""
    if previous == new:
        raise ValueError(f"the stack already runs {REPO}:{new}; nothing to switch")
    return [f"{REPO}:{previous}", f"{REPO}:{previous}-base", f"{REPO}:{new}-base"]


def main(argv):
    if len(argv) >= 3 and argv[1] == "current":
        with open(argv[2]) as f:
            print(current_tag(f.read()))
    elif len(argv) == 4 and argv[1] == "set":
        with open(argv[2]) as f:
            text = f.read()
        with open(argv[2], "w") as f:
            f.write(set_tag(text, argv[3]))
    elif len(argv) == 4 and argv[1] == "remove":
        for ref in remove_after_switch(argv[2], argv[3]):
            print(ref)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
