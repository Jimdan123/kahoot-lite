# Calibration source documents

Records what was actually uploaded through the running app to build the
Phase 1 human-labeled calibration set (`evals/human_labels.json`), so the
set's composition is documented and reproducible if it's ever redone or
extended. Uploaded 2026-08-03 via a dedicated test account
(`evals-calibration@example.com`) with `RATELIMIT_ENABLED=0` set locally
for the seeding session — normal rate limits apply otherwise.

All sources below are openly-licensed, genuine lecture notes/slides or
textbook excerpts (not syllabi, reading lists, or video-only pages).
Textbook-chapter picks are capped at ~10 pages each to keep per-document
LLM/chunk count bounded during calibration.

**Two substitutions from the original plan**, both logged here for
traceability:
- **Economics**: the original MIT OCW 14.01 *Fall 2018* course page
  404'd (site reorganized since original research) — substituted the
  live *Fall 2023* offering of the same course, same institution/subject.
- **History**: the two candidate UBC cIRcle theses were still unreachable
  (site blocks automated verification, confirmed again). The OpenStax
  *U.S. History* fallback was also dropped after checking its web reader
  page — it carries a footer notice ("This book may not be used in the
  training of large language models or otherwise be ingested into large
  language models or generative AI offerings without OpenStax's
  permission") that isn't present in the actual downloadable PDF's
  license text, but running the *scraped web-page content specifically*
  through this app's LLM pipeline was judged too close to what that
  notice restricts. Substituted a genuine MIT OCW History lecture-session
  PDF instead (21H.221, no such restriction).

| Subject | Document | Pages used | QuestionSet ID | Questions | Uploaded |
|---|---|---|---|---|---|
| Computer Science | MIT OCW 6.006 (Spring 2020), Lecture 2: Data Structures — https://ocw.mit.edu/courses/6-006-introduction-to-algorithms-spring-2020/79a07dc1cb47d76dae2ffedc701e3d2b_MIT6_006S20_lec2.pdf | all 5 pages | 41 | 17 | 2026-08-03 |
| Mathematics | MIT *A 2020 Vision of Linear Algebra* (Gilbert Strang) slides — https://ocw.mit.edu/courses/res-18-010-a-2020-vision-of-linear-algebra-spring-2020/mitres_18_010_s20_slides.pdf | pp. 1-10 of 44 | 42 | 6 | 2026-08-03 |
| Physics | OpenStax *College Physics 2e*, Ch. 1 "Introduction: The Nature of Science and Physics" — https://assets.openstax.org/oscms-prodcms/media/documents/College_Physics_2e-WEB.pdf | pp. 23-32 (ch. 1 spans 23-54) | 43 | 36 | 2026-08-03 |
| English | OpenStax *Writing Guide with Handbook*, Ch. 1 "The Digital World" — https://assets.openstax.org/oscms-prodcms/media/documents/WritingGuide-WEB.pdf | pp. 23-32 (ch. 1 spans 23-52) | 44 | 39 | 2026-08-03 |
| Economics | MIT OCW 14.01 (**Fall 2023**, substituted — see above), Lecture 1 — https://ocw.mit.edu/courses/14-01-principles-of-microeconomics-fall-2023/mit14_01_f23_lec1.pdf | all 3 pages | 45 | 3 | 2026-08-03 |
| Biology | MIT OCW 7.013 (Spring 2018), Recitation 1 — https://ocw.mit.edu/courses/7-013-introductory-biology-spring-2018/b7f9e5234a466680692aa5344fc9aca9_MIT7_013s18R1Q.pdf | all 3 pages | 46 | 14 | 2026-08-03 |
| History | MIT OCW 21H.221 (Fall 2006, **substituted** — see above), Session 10: "Was the Great Migration Great?" — https://ocw.mit.edu/courses/21h-221-the-places-of-migration-in-united-states-history-fall-2006/0d22d7a04fc6fcf3f0b9509ed873c6db_ses10_great_mig.pdf | all 5 pages | 47 | 27 | 2026-08-03 |

**Total: 142 questions across 7 QuestionSets**, ready for
`python -m evals.human_review --latest 7`.

## Notes on generation conditions

This batch ran with Groq fully unavailable (`403 Access denied — check
your network settings`, an IP/network-level block on the sandbox's
outbound datacenter IP — not an API key issue, won't occur in normal
local/production use). Every chunk fell back to NVIDIA/OpenRouter, several
of which struggled with malformed JSON on LaTeX-heavy content (the Math
source especially — see git history around 2026-08-03 for the
`run.py`/`wsgi.py` gevent monkey-patch fix made during this same session,
unrelated but discovered while debugging responsiveness during these
uploads). Despite the noisy generation, all 7 completed and produced
plausible, on-topic multiple-choice content — spot-checked manually.
