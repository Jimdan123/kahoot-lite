"""Context-enrichment tunables for the quiz-generation pipeline.

Only fires for documents that are mostly DEFINITIONS with little else to
pair them against — a document already rich in claims/mechanisms/
quantities never triggers a search, so this stays free for the common case.
"""
from __future__ import annotations

import os

THIN_DOCUMENT_DEFINITION_RATIO = 0.6     # doc counts as "thin" if definitions
                                          # make up >= this fraction of all
                                          # extracted record items
MIN_DEFINITIONS_FOR_ENRICHMENT = 3       # skip the ratio check below this —
                                          # too few items for a ratio to mean
                                          # anything
MAX_SEARCH_RESULTS = 5                   # Tavily results requested per query

# Second, independent trigger: a document can look "rich" (plenty of
# claims/mechanisms, not thin) and STILL fail every attempt if the topic is
# just canonical enough that any two-hop combination drawn from it is still
# guessable by a closed-book model — confirmed against a real ADT/stack/
# queue lecture where 9/9 drafts leaked on every retry despite a
# non-thin comprehension record. When the first attempt's closed-book leak
# rate is this high, retry WITH external context even though the thin-doc
# check alone said no.
HIGH_LEAK_RATIO = 0.7                    # retry-with-search if this fraction (or
                                          # more) of drafts leaked closed-book
MIN_DRAFTS_FOR_LEAK_CHECK = 3            # skip the leak-rate check below this —
                                          # too few drafts for a ratio to mean
                                          # anything


def has_search_enrichment() -> bool:
    return bool(os.environ.get('TAVILY_API_KEY'))
