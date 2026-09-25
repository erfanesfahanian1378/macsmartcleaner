## What does this change?

## For new rules
- [ ] Tested on a real Mac (`./msc scan` shows it, `./msc clean --dry-run --only <id>` looks right)
- [ ] Safety tier chosen conservatively; `why` and `impact` written in plain English
- [ ] Test added in `tests/`

## Checks
- [ ] `python3 -m unittest discover -s tests -t .` passes
