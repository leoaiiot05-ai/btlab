# Cloud sessions

Read this when a Claude Code cloud session is asked to continue the btlab build: from `claude --cloud`, from the Claude app or claude.ai/code, or by the scheduled task.

- On the owner's Windows PC, `run_prompts.py` runs the same build, and its sessions ignore this file.
- The PC and the cloud share state only through git, on `master` at GitHub:
  - each finished prompt is a commit `Prompt N: <title>`, and its plan is in `plans/`;
  - `buildctl/baton.json` says who may build: `local` (the PC) or `cloud`;
  - `buildctl/lease.json` says which cloud session is building, and when it last showed signs of life.
- Every git change in a cloud session goes through `python3 buildctl/cloud_lease.py`. Never run `git commit`, `push`, `pull`, `merge`, `reset`, `checkout`, `stash` or `rebase` yourself; the repository settings deny them. Read-only git commands are fine.

## Start of every session

1. Run `python3 buildctl/cloud_lease.py take`.
   - Exit 0: you hold the lease. Note the session id it prints.
   - Any other exit code: print the script's message and stop at once, changing nothing. This is the normal outcome when the PC holds the baton, when another session is working, and when the build is finished.
2. Set up the environment as "This VM is not the owner's PC" says.
3. Work through the prompts as the next sections say.

If a later command exits with 15 (no session id), for example after the VM was replaced, run `python3 buildctl/cloud_lease.py take --session <your id>` once, then repeat the command.

## Scheduled run

A scheduled task starts a session every 2 hours with the prompt "Read CLOUD.md and follow its Scheduled run section." In such a session:

1. Do "Start of every session". When `take` exits with anything but 0, stop immediately. That is the normal outcome of most scheduled runs.
2. Otherwise continue the build exactly as below.

## Where the build stands

- A prompt is done when `git log --format=%s` shows a commit whose subject starts with `Prompt N:`. A subject that starts with `WIP prompt N:` marks unfinished work.
- Work through PROMPTS.md in order, starting at the first prompt that is not done. Stop when every prompt is done, or when the script tells you to stop.
- If `plans/prompt-NN.md` already exists, an earlier session, on the PC or in the cloud, started that prompt. Continue from its plan and the current files instead of starting over.

## Rules for each prompt

Nobody is watching this session. These rules replace "Plan first ... wait for my approval" in PROMPTS.md and the approval step of the Planning section in CLAUDE.md.

1. If the plan file does not exist, write the plan, in the format CLAUDE.md requires, to `plans/prompt-NN.md` before changing any code, and append its deferred items to TODO.md. Then run `python3 buildctl/cloud_lease.py checkpoint "plan"`.
2. Implement it without waiting and without asking questions. Anything the prompt asks you to give the owner, such as a checklist or a report, also goes into the plan file.
3. After each major step, run `python3 buildctl/cloud_lease.py checkpoint "<step>"`.
   - Also run `checkpoint`, or `python3 buildctl/cloud_lease.py beat`, at least every 45 minutes (check with `date -u`), including while you wait for a long workflow. A lease without a heartbeat for 4 hours counts as abandoned, and another session may take over.
4. Never start the app (`python -m btlab`, `python -m btlab.bench`), never contact Tiingo, FinMind, FRED, spylead or ibguard, and never connect to IB. Prove behaviour with tests.
5. Finish with the full test suite passing: `.venv/bin/python -m pytest -q`.
6. Run `python3 buildctl/cloud_lease.py finish N`. It commits everything with the subject `Prompt N: <title as written in PROMPTS.md>` and pushes it.
   - Exit 0: continue with the prompt it names.
   - Exit 11: every prompt is done. Stop.
7. If something blocks you, such as a contradiction in SPEC.md, a denied command, or a dependency CLAUDE.md does not approve:
   - describe it under a `## Blocked` heading in the plan file;
   - run `python3 buildctl/cloud_lease.py release "blocked"`, and stop;
   - never weaken a rule to get past it.

   Remove a `## Blocked` section left by an earlier session only once the blocker no longer holds. If it still holds, run `release "blocked"` and stop, changing nothing else.

Prompt 25 is the audit: write the whole report to `plans/prompt-25.md`, change no other file, then run `finish 25`.

## When the script says stop

| Exit | Meaning | What the script already did |
|---|---|---|
| 10 | The PC holds the baton | Pushed your work as a WIP commit and released your lease, if you held it |
| 11 | Every prompt is committed | Released the lease |
| 12 | Another session holds the lease | Nothing; you changed nothing yet |
| 13 | Another session took your lease | Pushed your work to the rescue branch it names |
| 14 | Git failed: a merge conflict, or a push that kept failing | Pushed what it could to the rescue branch it names |

After any of these, stop. For 13 and 14, end with the script's message, so the owner sees the branch.

Before stopping for any other reason (the session is ending, a usage limit), run `python3 buildctl/cloud_lease.py release "<reason>"`. The next session can then start at once instead of waiting 4 hours for the lease to go stale.

## This VM is not the owner's PC

- The VM runs Linux and the owner runs Windows 11. Where CLAUDE.md says `.venv/Scripts/python`, use `.venv/bin/python`. Keep Windows-only code guarded, as CLAUDE.md requires.
- `pylock.toml` is generated on the PC and is valid only there. Never install from it, regenerate it or edit it here.
  - For tests: `python3 -m venv .venv`, then `.venv/bin/python -m pip install -r requirements.in`.
  - This exception to "install only from the lock" applies to this VM only.
- Prompt 1 generates the lock, so it runs on the PC; `take` refuses to start before prompt 1 is done.
- There is no `config.toml`, IB Gateway, ibguard or spylead here, and none is needed: the tests use fakes.
- The manual checks named in prompts 17, 18, 19, 21, 23 and 24 happen later on the PC. Do the code, the tests and the checklists in the plan only.

## Git

- The script pushes every commit to `master` as soon as it makes it. The session can stop at any moment, for example at a usage limit or when an idle VM is reclaimed, and only pushed work survives.
- Never force-push or rewrite history. Never commit `.venv/`, `config.toml`, keys, `data/` or `logs/`.
- Never edit `buildctl/` by hand.
