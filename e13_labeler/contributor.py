"""
The contributor agreement every non-owner accepts at first login (FR-60, NFR-4).

Bump VERSION whenever TEXT changes: everyone is asked to accept again.
The licence of contributed labels is still an open owner question (§11 Q4), so
this text is a draft until the owner settles it.
"""

VERSION = "1-draft"

TEXT = """\
## Contributor agreement (version 1, draft)

**What your labels are for.** You are labelling why a question can't be answered
from a short text ("abstain reasons") and marking the words that show it. The
labels are used to train and evaluate a language model and to measure how far
labelers agree. They may be published as part of a dataset.

**How you appear.** Your labels are stored and exported under a pseudonym (such
as L07). Your login name is not exported. Any contact details you give the
project owner are kept separately and never exported.

**Time tracking.** The app records how long each item is open and how long you
are actively working on it (the tab is visible and you interacted in the last
60 seconds). These times are used to estimate cost and to spot rushing.

**Quality checks.** Some items have known answers and are mixed in unannounced.
Your accuracy on them is tracked; a run of low accuracy pauses your account
until you retake the quiz.

**Licence of your labels.** To be confirmed by the project owner (open question
§11 Q4). You will be asked to accept again when it is settled.

**Confidential text.** Some items come from restricted sources. Don't copy,
store or share item text outside this app.
"""
