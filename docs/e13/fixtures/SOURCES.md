# Fixture sources and attributions

`pool_eval_libre_sample.jsonl` holds 384 rows from the 44 sources below. All are class `libre` in
`docs/e13/source_permissions.json` (a copy of ModernBERT-NLI-Advanced `experiments/v3/release/source_permissions.json`,
web-verified 2026-10-05). The licence column is copied verbatim from that file.

**The fixture file is not under one licence.** Each row keeps the licence of its `source`. Rows from
share-alike sources (CC-BY-SA, CDLA-Sharing, and the share-alike genres of MultiNLI) carry attribution and
share-alike terms. If you redistribute the file, or any derivative that contains their text, those rows must stay
under the same terms and credit the original authors. For attribution-only sources (CC-BY, ODC-By, AFL), credit the
authors. For MIT and Apache-2.0, keep the licence and copyright notice. The pool's own builders (E09 in
ModernBERT-NLI-Advanced) are Apache-2.0, but that does not relicense the text.

| source | rows | licence (from `source_permissions.json`) | attribution / share-alike |
|---|---:|---|---|
| `amazon_stars` | 8 | Apache-2.0 (SetFit mirror) | Keep the licence and copyright notice |
| `arc` | 8 | CC-BY-SA-4.0 | **Attribution + share-alike** (CC-BY-SA-4.0) |
| `arena_pref` | 8 | Apache-2.0 | Keep the licence and copyright notice |
| `banking77` | 8 | MIT / CC-BY-4.0 | Attribution (keep the MIT notice and credit the authors per CC-BY-4.0) |
| `bias_in_bios` | 8 | MIT | Keep the licence and copyright notice |
| `bitext_support` | 8 | CDLA-Sharing-1.0 | **Attribution + share-alike** (CDLA-Sharing-1.0: redistributed data stays under CDLA-Sharing) |
| `boolq` | 8 | CC-BY-SA-3.0 | **Attribution + share-alike** (CC-BY-SA-3.0) |
| `civil_comments` | 8 | CC0-1.0 | None required (public-domain dedication); citing is courtesy |
| `clinc_oos` | 8 | CC-BY-3.0 | Attribution |
| `commonsense_qa` | 8 | MIT | Keep the licence and copyright notice |
| `dbpedia` | 8 | CC-BY-SA-3.0 | **Attribution + share-alike** (CC-BY-SA-3.0) |
| `dbpedia_l2` | 8 | CC0-1.0 | None required (public-domain dedication); citing is courtesy |
| `dbpedia_l3` | 8 | CC0-1.0 | None required (public-domain dedication); citing is courtesy |
| `fever` | 8 | CC-BY-SA-3.0 | **Attribution + share-alike** (CC-BY-SA-3.0) |
| `glaive_tools` | 8 | Apache-2.0 | Keep the licence and copyright notice |
| `hellaswag` | 8 | MIT | Keep the licence and copyright notice |
| `helpsteer2` | 8 | CC-BY-4.0 | Attribution |
| `helpsteer3_pref` | 8 | CC-BY-4.0 | Attribution |
| `hermes_tools` | 8 | Apache-2.0 | Keep the licence and copyright notice |
| `hh_rlhf` | 8 | MIT | Keep the licence and copyright notice |
| `liar2` | 8 | Apache-2.0 | Keep the licence and copyright notice |
| `medmcqa` | 8 | Apache-2.0 | Keep the licence and copyright notice |
| `medqa` | 8 | CC-BY-4.0 | Attribution |
| `mind2web` | 8 | CC-BY-4.0 | Attribution |
| `mmlu` | 8 | MIT | Keep the licence and copyright notice |
| `mnli` | 8 | mixed per genre (OANC, CC-BY, CC-BY-SA, PD) | **Attribution + share-alike** (treat every row as SA: some genres are CC-BY-SA-3.0, others CC-BY-3.0, OANC or public domain; the row does not record its genre) |
| `openbookqa` | 8 | Apache-2.0 (allenai/OpenBookQA repo, verified 2026-10-05) | Keep the licence and copyright notice |
| `piqa` | 8 | Academic Free License 3.0 (yonatanbisk.com/piqa, verified 2026-10-05) | Attribution (keep the AFL-3.0 notice) |
| `pubmedqa` | 8 | MIT | Keep the licence and copyright notice |
| `qasc` | 8 | CC-BY-4.0 | Attribution |
| `reward_bench` | 8 | ODC-By | Attribution (ODC-By) |
| `snli` | 8 | CC-BY-SA-4.0 | **Attribution + share-alike** (CC-BY-SA-4.0) |
| `strategyqa` | 8 | MIT | Keep the licence and copyright notice |
| `synth` | 8 | Apache-2.0 (decider repo) | Keep the licence and copyright notice |
| `toolace` | 8 | Apache-2.0 | Keep the licence and copyright notice |
| `truthfulqa` | 8 | Apache-2.0 | Keep the licence and copyright notice |
| `typed_decisions` | 40 | Apache-2.0 | Keep the licence and copyright notice |
| `ultrafeedback_pref` | 8 | MIT | Keep the licence and copyright notice |
| `winogrande` | 8 | CC-BY (allenai/winogrande README, verified 2026-10-05) | Attribution |
| `x_mnli_mm` | 8 | mixed per genre | **Attribution + share-alike** (treat every row as SA: some genres are CC-BY-SA-3.0, others CC-BY-3.0, OANC or public domain; the row does not record its genre) |
| `x_snli` | 8 | CC-BY-SA-4.0 | **Attribution + share-alike** (CC-BY-SA-4.0) |
| `x_vitaminc` | 8 | CC-BY-SA-3.0 | **Attribution + share-alike** (CC-BY-SA-3.0) |
| `x_wanli` | 8 | CC-BY-4.0 (card; GPT-3 generations) | Attribution (GPT-3-generated text; see below) |
| `xstory_cloze` | 8 | CC-BY-SA-4.0 | **Attribution + share-alike** (CC-BY-SA-4.0) |
| **total** | **384** | | |

## Libre sources left out

These sources are `libre` by licence, but their text comes from a platform with its own terms, or there is doubt
about that. The builder's `EXCLUDE` set and its licence-note regex (`reddit|twitter|per-task`) drop them.

| source | why |
|---|---|
| `go_emotions` | Reddit comments (the licence note says "Apache-2.0 (Reddit text)") |
| `fin_sentiment` | Tweets: the states contain `t.co` links (Twitter financial-news sentiment), and the MIT licence covers the labels, not the tweet text |
| `hate_speech_scales` | Social-media comments from YouTube, Reddit, Twitter and Gab (Measuring Hate Speech; samples begin "NTA ...") |
| `prosocial_safety` | In doubt: ProsocialDialog contexts are seeded from Social Chemistry situations, which are drawn partly from Reddit |

All `restricted` and `unverified` sources are left out as well. The builder reads the class from the permissions
file, so a re-run after a reclassification picks the change up.

## Model-generated text (kept, flagged)

`x_wanli` (GPT-3 generations), `arena_pref`, `ultrafeedback_pref`, `helpsteer2`, `helpsteer3_pref`, `hh_rlhf`,
`reward_bench`, `synth` and `typed_decisions` contain LLM-written text. Their dataset licences are permissive, and the
owner classed them `libre`, so they are kept. Any generator terms of service bind whoever generated the text, not
this fixture. The owner may want to review this before a public release.

