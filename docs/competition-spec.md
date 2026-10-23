# Competition specs

A competition is still a directory of files (`Compfile`, `boxes.json`, `box_services.json`,
`box_vulns.json`, `users.json`). The interactive picker (`create-competition.py --new`) asks
for those boxes one at a time, and `run-agent-scrim.py --new --from-template` copies an
existing one. For a new format, describe it once in JSON and generate the directory:

```bash
python3 generate-competition.py spec.json \
    --comp-dir "$TEZ_COMPETITIONS_ROOT/my-comp"   # default: $TEZ_COMPETITIONS_ROOT/<name>
```

The output is a normal competition: deploy it with
`create-competition.py --comp-dir <dir> --teams N --yes`, or submit it through the dashboard
(the dashboard reads the same competitions root).

## Spec

```json
{
  "name": "mixed-3box",
  "scenario": "Two Linux services and a Windows file drop",
  "difficulty": 3,
  "box_username": "ubuntu",
  "credlist_usernames": ["lead", "records", "audit"],
  "boxes": [
    {"name": "web01", "os": "debian", "services": ["nginx", "ssh"]},
    {"name": "db01", "os": "ubuntu", "cpu": 2, "memory_mb": 4096,
     "services": ["mysql", "ssh"]},
    {"name": "win01", "os": "windows",
     "services": ["IIS FTP",
                  {"name": "score/tcp", "score_only": true, "check": "Tcp",
                   "display": "ftp", "port": 21}]}
  ],
  "vulns": {"web01": ["suid-find"]},
  "compfile": {"red.c2_engine": "sliver", "red.c2_implant_spread": "full"}
}
```

- `os` accepts `debian`, `ubuntu`, `windows`/`win`, or `fedora`; these map to the current
  base templates. `template` overrides the mapping for a one-off image.
- Omit `name` and the boxes are named `box01`, `box02`, …; omit `last_octet` and they take
  `.2`, `.3`, … in order. `last_octet`, `cpu` (default 1), `memory_mb` (default 2048),
  `disk_gb` (default: the template's disk) and `disk_iface` are optional.
- `services` entries are nakon service ids, or full pin objects (`plant_only`,
  `score_only`, `check`, `display`, `port`, `vars`). An FTP service that means "port 21 is
  reachable" needs the `plant_only` + `score/tcp` pair shown above; a bare `IIS FTP` is a
  protocol-login check and can score DOWN even while `:21` listens.
- `vulns` is optional and keyed by box name; a box can also carry its own `vulns` list.
- `compfile` adds keys such as `red.c2_engine` and `red.c2_implant_spread` after the base
  name/scenario/difficulty/remote-access keys.
- `box_username` and the three `credlist_usernames` default to `ubuntu` and
  `admin,user1,user2`.

Use `count` instead of repeating identical boxes:

```json
{"boxes": [
  {"count": 2, "os": "windows", "services": ["IIS FTP"]},
  {"count": 2, "os": "debian", "services": ["ssh", "nginx"]}
]}
```

generates `box01`…`box04` with sequential octets.

`--force` overwrites an existing non-empty competition directory. The generated
`box_services.json` and `box_vulns.json` are the inputs the deploy pins; they are not
randomized afterwards.
