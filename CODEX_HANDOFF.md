# CODEX_HANDOFF.md

## 当前目标

Release v0.1.0: operator workbench source, bilingual Pages, portable Windows client and recorded evidence replay.

## 项目关键约束

* Original code/docs MIT; preserve third-party notices. Only approved demo exports are public.
* 4090 owns formal development/releases. PC provides thin-client access and Windows packaging/QA.
* Publication does not start motion, clear STOP, recalibrate or fabricate acceptance.
* Controller source snapshots require installation-specific environments and new runtime binding.
* Preview: `python -m http.server 8933 --directory site`.
* Checks: `python -B -m unittest discover -s tests -p 'test_*.py'`; `node --test tests/test_ui*.mjs`.

## 已修改文件

* client/sdk.py: portable launcher, offline replay and explicit SSH connection.
* site and generators: English/Chinese pages, actual operator-source preview, shared-clock measured replay.
* lab_stack: production source with current async recording/flush changes.
* controllers and inference: audited snapshots, attribution and publication-only example path substitutions.
* AGENTS.md: commit each completed change; verify main/Pages publication.

## 已完成验证

* Initial Windows ZIP freshly extracted, 162 file hashes verified, bundled Python/OpenSSH executed, offline HTTP and unauthorized connection rejection checked. The final release receipt supersedes this initial-package count.
* Native camera/state references and original USB hashes checked. Video full-decode checked; measured replay does not call robot code.

## 当前问题

* A full new-robot installation and hardware cold start are not validated by this software publication.
* Whole-machine v2 asset redistribution terms are incomplete; official source is linked and the asset excluded.
* The original example trial ended on an evidence-queue error and is not a benchmark success.

## 下一步

* Make later changes on a separate codex branch, with a reviewable commit/PR; follow docs/development.md.
* DAgger is maintained in another worktree. Do not overwrite or silently merge it.

## 不要重复做

* Do not add private profiles, raw trials, weights, credentials or runtime acceptance to Git.
* Do not present single-arm kinematic replay as a calibrated full-machine simulator.
