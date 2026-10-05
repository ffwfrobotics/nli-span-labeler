#!/usr/bin/env python3
"""
NLI Span Labeler - A web-based tool for annotating NLI examples with span-level labels.

Features:
- WordPiece tokenization matching ModernBERT training target
- Word-level span selection with position tracking
- Pre-filled labels for difficulty dimensions and NLI relations
- Custom label creation with unique colors
- Multiple labels per token visualization
- Complexity scoring (0-10 scale)
- SQLite persistence
- Stats dashboard and export
- Multi-user support with session-based authentication
- Annotator tracking for all labels and scores
- Label schema enforcement with custom label tracking
- Example locking for concurrent multi-user annotation
- Role-based admin mode with protected endpoints
- Inter-annotator agreement metrics and question pools

Usage:
    cd nli-span-labeler
    uvicorn app:app --reload --port 8000
    # Then open http://localhost:8000

Environment Variables:
    ANONYMOUS_MODE=1  - Disable auth, use anonymous user (for local single-user)
    ALLOW_ANONYMOUS_SUBMISSIONS=1  - Allow logged-out users to submit (modal dismissable)
    TOKENIZER_MODEL=answerdotai/ModernBERT-base  - HuggingFace model for tokenizer
    LOCK_TIMEOUT_MINUTES=30  - How long example locks last (default: 30)
    ADMIN_USER=username  - Bootstrap admin user (gets admin role on startup)
    CONSENSUS_THRESHOLD=10  - Annotations needed before consensus calculation (default: 10)
    CALIBRATION_MIN_SCORED=5  - Scored annotations needed for "calibrated" status (default: 5)
    AGREEMENT_HIGH_THRESHOLD=0.8  - Agreement score to promote to test pool (default: 0.8)
    CALIBRATION_ROUTING_ENABLED=1  - Enable stealth calibration routing (default: 1)
    CALIBRATION_TEST_RATIO_NEW=0.8  - Test question ratio for new/provisional users (default: 0.8)
    CALIBRATION_TEST_RATIO_ESTABLISHED=0.1  - Test question ratio for calibrated users (default: 0.1)
    CONTROVERSIAL_THRESHOLD=0.5  - Auto-flag examples with agreement below this (default: 0.5)

Rate Limiting:
    RATE_LIMIT_ENABLED=1  - Enable/disable rate limiting (default: enabled)
    RATE_LIMIT_AUTH=5/minute  - Limit for auth endpoints (login, register)
    RATE_LIMIT_ANNOTATION=30/minute  - Limit for annotation endpoints (next, label, skip)
    RATE_LIMIT_DEFAULT=60/minute  - Limit for all other endpoints

CORS & Logging:
    CORS_ORIGINS=https://example.com  - Comma-separated allowed origins (default: same-origin only)
    CORS_ALLOW_CREDENTIALS=1  - Allow credentials in CORS requests (default: 1)
    REQUEST_LOG_LEVEL=INFO  - Logging level for request logs (default: INFO)
    REQUEST_LOG_FILE=/path/to/requests.log  - Optional file for request logs (default: stdout only)
"""

import json
import sqlite3
import secrets
import hashlib
import os
import random
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
from contextlib import contextmanager
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Query, Request, Response, Depends, Cookie, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import time
import logging

# Tokenizer - using the actual training target model
from transformers import AutoTokenizer

# Paths - relative to this file's directory
APP_DIR = Path(__file__).parent.resolve()
DATA_DIR = APP_DIR / "data" / "nli"
DB_PATH = APP_DIR / "labels.db"

# Ensure data directory exists
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Configuration
ANONYMOUS_MODE = os.environ.get("ANONYMOUS_MODE", "0") == "1"
ALLOW_ANONYMOUS_SUBMISSIONS = os.environ.get("ALLOW_ANONYMOUS_SUBMISSIONS", "0") == "1"  # Allow logged-out users to submit

# CORS configuration - comma-separated list of allowed origins (default: same-origin only)
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "").strip()
CORS_ALLOW_CREDENTIALS = os.environ.get("CORS_ALLOW_CREDENTIALS", "1") == "1"

# Request logging configuration
REQUEST_LOG_LEVEL = os.environ.get("REQUEST_LOG_LEVEL", "INFO").upper()
REQUEST_LOG_FILE = os.environ.get("REQUEST_LOG_FILE", "")  # Empty = stdout only

# Set up request logger
request_logger = logging.getLogger("nli.requests")
request_logger.setLevel(getattr(logging, REQUEST_LOG_LEVEL, logging.INFO))
if not request_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(handler)
    if REQUEST_LOG_FILE:
        file_handler = logging.FileHandler(REQUEST_LOG_FILE)
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        request_logger.addHandler(file_handler)
SESSION_EXPIRY_DAYS = 30
ANONYMOUS_USER_ID = 1
LEGACY_USER_ID = 2
TOKENIZER_MODEL = os.environ.get("TOKENIZER_MODEL", "answerdotai/ModernBERT-base")
LOCK_TIMEOUT_MINUTES = int(os.environ.get("LOCK_TIMEOUT_MINUTES", "30"))
ADMIN_USER = os.environ.get("ADMIN_USER", "")  # Username to bootstrap as admin

# Role constants
ROLE_ADMIN = "admin"
ROLE_ANNOTATOR = "annotator"

# Agreement configuration
CONSENSUS_THRESHOLD = int(os.environ.get("CONSENSUS_THRESHOLD", "10"))
# Reliability scoring configuration
CALIBRATION_MIN_SCORED = int(os.environ.get("CALIBRATION_MIN_SCORED", "5"))  # Min scored annotations to be "calibrated"
RELIABILITY_COMPLEXITY_WEIGHT = 0.6  # Weight for complexity agreement in reliability score
RELIABILITY_SPAN_WEIGHT = 0.4  # Weight for span agreement in reliability score

AGREEMENT_HIGH_THRESHOLD = float(os.environ.get("AGREEMENT_HIGH_THRESHOLD", "0.8"))

# Calibration routing configuration
CALIBRATION_ROUTING_ENABLED = os.environ.get("CALIBRATION_ROUTING_ENABLED", "1") == "1"
CALIBRATION_TEST_RATIO_NEW = float(os.environ.get("CALIBRATION_TEST_RATIO_NEW", "0.8"))  # 80% test questions for new users
CALIBRATION_TEST_RATIO_ESTABLISHED = float(os.environ.get("CALIBRATION_TEST_RATIO_ESTABLISHED", "0.1"))  # 10% for drift detection

# Rate limiting configuration
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "1") == "1"
RATE_LIMIT_AUTH = os.environ.get("RATE_LIMIT_AUTH", "5/minute")  # Auth endpoints
RATE_LIMIT_ANNOTATION = os.environ.get("RATE_LIMIT_ANNOTATION", "30/minute")  # Annotation endpoints
RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "60/minute")  # Everything else

# Training configuration
TRAINING_ENABLED = os.environ.get("TRAINING_ENABLED", "1") == "1"
TRAINING_MIN_EXAMPLES = int(os.environ.get("TRAINING_MIN_EXAMPLES", "5"))  # Gold examples required to complete training


def get_real_ip(request: Request) -> str:
    """
    Get the real client IP, respecting X-Forwarded-For for reverse proxy setups.

    Priority:
    1. X-Forwarded-For header (first IP in chain)
    2. X-Real-IP header
    3. Direct client IP
    """
    # X-Forwarded-For can be comma-separated list, first is original client
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    # Some proxies use X-Real-IP
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Fall back to direct client
    return get_remote_address(request)


# Initialize rate limiter with proxy-aware key function
limiter = Limiter(
    key_func=get_real_ip,
    enabled=RATE_LIMIT_ENABLED,
    default_limits=[RATE_LIMIT_DEFAULT],
)

# Controversial example flagging configuration
CONTROVERSIAL_THRESHOLD = float(os.environ.get("CONTROVERSIAL_THRESHOLD", "0.5"))  # Auto-flag when agreement below this

# ============================================================================
# Tiered Annotation System
# ============================================================================

# Tier 0: Fully automatic labels (computed deterministically)
TIER0_WORDLISTS = {
    "negation": {
        "not", "no", "never", "nobody", "nothing", "nowhere", "neither", "none",
        "n't", "cannot", "can't", "won't", "don't", "doesn't", "didn't", "isn't",
        "aren't", "wasn't", "weren't", "hasn't", "haven't", "hadn't", "without",
        "lack", "lacking", "fail", "failed", "refuse", "refused"
    },
    "quantifier": {
        "all", "every", "each", "any", "some", "most", "many", "few", "several",
        "both", "none", "no", "half", "majority", "minority", "numerous", "countless"
    },
    "modal": {
        "might", "may", "could", "would", "should", "must", "can", "will", "shall", "ought"
    },
    "conditional": {
        "if", "unless", "when", "whenever", "provided", "assuming", "suppose",
        "supposing", "whether", "in case", "as long as"
    },
    "temporal": {
        "before", "after", "during", "while", "when", "until", "since", "always",
        "never", "sometimes", "often", "rarely", "occasionally", "frequently",
        "constantly", "already", "yet", "still", "now", "then", "soon", "later",
        "earlier", "recently", "formerly", "previously"
    },
    "causal": {
        "because", "since", "therefore", "thus", "hence", "consequently", "so",
        "as a result", "due to", "owing to", "caused", "causes", "causing",
        "leads to", "results in"
    }
}

# Tier 1: Weak supervision labels (auto-generated, spot-checked)
# Requires NLTK WordNet for semantic relations
TIER1_LABELS = ["hypernym", "hyponym", "synonym", "antonym", "meronym", "holonym", "numeric_mismatch", "entity_alignment"]

# WordNet initialization flag
_wordnet_initialized = False

def ensure_wordnet_data():
    """Download WordNet data if not already present."""
    global _wordnet_initialized
    if _wordnet_initialized:
        return True
    try:
        import nltk
        from nltk.corpus import wordnet
        # Test if data is available
        wordnet.synsets("test")
        _wordnet_initialized = True
        return True
    except LookupError:
        try:
            import nltk
            nltk.download("wordnet", quiet=True)
            nltk.download("omw-1.4", quiet=True)  # Open Multilingual WordNet
            _wordnet_initialized = True
            return True
        except Exception as e:
            print(f"Warning: Could not download WordNet data: {e}")
            return False
    except ImportError:
        print("Warning: NLTK not installed, Tier 1 labels unavailable")
        return False

# Tier 1 configuration
TIER1_QUALITY_SAMPLE_RATE = float(os.environ.get("TIER1_QUALITY_SAMPLE_RATE", "0.05"))  # 5% spot-check rate
TIER1_MIN_PRECISION = float(os.environ.get("TIER1_MIN_PRECISION", "0.90"))  # 90% precision threshold

# ============================================================================
# Label Schema Configuration
# ============================================================================

# System-defined labels organized by category
# These are the "official" labels that get is_custom=False
# Any label not in this list will be marked is_custom=True
SYSTEM_LABELS = {
    "difficulty": {
        "description": "Difficulty dimension labels for complexity analysis",
        "labels": [
            {"name": "reasoning", "color": "#3b82f6", "description": "Requires logical inference"},
            {"name": "creativity", "color": "#8b5cf6", "description": "Requires imaginative interpretation"},
            {"name": "domain_knowledge", "color": "#06b6d4", "description": "Requires specialized expertise"},
            {"name": "contextual", "color": "#22c55e", "description": "Depends on implicit context"},
            {"name": "constraints", "color": "#eab308", "description": "Multiple conditions to track"},
            {"name": "ambiguity", "color": "#f97316", "description": "Answer is debatable"},
        ]
    },
    "nli": {
        "description": "Natural Language Inference relation labels",
        "labels": [
            {"name": "entailment", "color": "#4ade80", "description": "Marks tokens supporting entailment"},
            {"name": "neutral", "color": "#fbbf24", "description": "Marks tokens indicating neutrality"},
            {"name": "contradiction", "color": "#f87171", "description": "Marks tokens showing contradiction"},
        ]
    }
}

# Flattened set of all system label names for quick lookup
SYSTEM_LABEL_NAMES = set()
for category in SYSTEM_LABELS.values():
    for label in category["labels"]:
        SYSTEM_LABEL_NAMES.add(label["name"])


def is_system_label(label_name: str) -> bool:
    """Check if a label name is a system-defined label."""
    return label_name.lower() in SYSTEM_LABEL_NAMES


def get_label_schema() -> dict:
    """Get the complete label schema with metadata."""
    return {
        "categories": SYSTEM_LABELS,
        "system_labels": sorted(SYSTEM_LABEL_NAMES),
        "allow_custom": True,
        "custom_tracking": True,
    }


# Global tokenizer - initialized at startup
_tokenizer = None

# ============================================================================
# API Documentation
# ============================================================================

API_DESCRIPTION = """
# NLI Span Labeler API

A multi-user annotation tool for Natural Language Inference (NLI) examples with span-level labeling.

## Features

- **Span-level annotation**: Select specific tokens in premise/hypothesis pairs
- **Multiple labels per token**: Annotate tokens with multiple semantic labels
- **Complexity scoring**: Rate examples on 6 difficulty dimensions (0-10 scale)
- **Multi-user support**: Session-based authentication with per-user annotation tracking
- **Role-based access**: Admin and annotator roles with protected endpoints
- **WordPiece tokenization**: Uses ModernBERT tokenizer for model-aligned annotations
- **Label schema enforcement**: System labels tracked separately from custom labels
- **Concurrent annotation**: Example locking prevents duplicate work
- **Inter-annotator agreement**: Track consensus and annotator reliability

## Authentication

Most endpoints require authentication via session cookie. Use the `/api/auth/login` or 
`/api/auth/register` endpoints to obtain a session.

Set `ANONYMOUS_MODE=1` environment variable to disable authentication for local single-user usage.

## Admin Mode

Set `ADMIN_USER=username` environment variable to bootstrap a user as admin on startup.
Admins have access to additional endpoints under `/api/admin/` for user management and
system-wide operations.

## Example Locking

When you request an example via `/api/next`, it's automatically locked to you for 30 minutes.
Other users won't receive the same example. Locks are released when you save or skip, or
you can explicitly release via `/api/lock/release/{example_id}`.

## Question Pools

Examples are categorized into pools:
- **test**: High-consensus examples used for calibrating new annotators
- **building**: Being actively annotated, need more responses
- **zero_entry**: Unannotated examples

## Label Schema

The system defines official labels in two categories:
- **Difficulty**: reasoning, creativity, domain_knowledge, contextual, constraints, ambiguity
- **NLI**: entailment, neutral, contradiction

Custom labels are allowed but tracked with `is_custom=True` for data quality analysis.

## Datasets

Place JSONL files in `data/nli/` directory. Expected format:
```json
{"id": "example_1", "premise": "...", "hypothesis": "...", "label": 0, "label_text": "entailment"}
```
"""

TAGS_METADATA = [
    {
        "name": "Authentication",
        "description": "User registration, login, logout, and session management.",
    },
    {
        "name": "Admin",
        "description": "Admin-only endpoints for user management and system configuration. Requires admin role.",
    },
    {
        "name": "Examples",
        "description": "Retrieve NLI examples for annotation. Examples are served per-user to avoid duplicate annotations.",
    },
    {
        "name": "Annotation",
        "description": "Submit span labels and complexity scores for examples.",
    },
    {
        "name": "Locking",
        "description": "Manage example locks for concurrent annotation.",
    },
    {
        "name": "Agreement",
        "description": "Inter-annotator agreement metrics and question pool management.",
    },
    {
        "name": "Reliability",
        "description": "Annotator reliability scoring and leaderboard data.",
    },
    {
        "name": "Statistics",
        "description": "View annotation statistics, export data, and manage annotators.",
    },
    {
        "name": "System",
        "description": "System information endpoints (tokenizer, datasets, labels, health).",
    },
]

app = FastAPI(
    title="NLI Span Labeler API",
    description=API_DESCRIPTION,
    version="1.4.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=TAGS_METADATA,
    contact={
        "name": "GoblinCorps",
        "url": "https://github.com/GoblinCorps/nli-span-labeler",
    },
    license_info={
        "name": "MIT",
        "url": "https://opensource.org/licenses/MIT",
    },
)

# Attach rate limiter to app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Mount static files
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ============================================================================
# CORS Middleware
# ============================================================================

def get_cors_origins() -> list[str]:
    """Parse CORS_ORIGINS environment variable into list of allowed origins."""
    if not CORS_ORIGINS:
        return []  # No origins = same-origin only (restrictive default)
    return [origin.strip() for origin in CORS_ORIGINS.split(",") if origin.strip()]

cors_origins = get_cors_origins()
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=CORS_ALLOW_CREDENTIALS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )


# ============================================================================
# Request Logging Middleware
# ============================================================================

def get_client_ip(request: Request) -> str:
    """
    Get client IP, respecting X-Forwarded-For for proxy deployments.

    X-Forwarded-For format: client, proxy1, proxy2, ...
    We want the leftmost (original client) IP.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Take the first IP in the chain (original client)
        return forwarded.split(",")[0].strip()
    # Fall back to direct connection IP
    if request.client:
        return request.client.host
    return "unknown"


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Log all requests with timing, IP, and user info."""
    start_time = time.time()

    # Get client IP (proxy-aware)
    client_ip = get_client_ip(request)

    # Process request
    response = await call_next(request)

    # Calculate duration
    duration_ms = (time.time() - start_time) * 1000

    # Try to get user ID from session (best effort)
    user_id = "anon"
    session_id = request.cookies.get("session_id")
    if session_id:
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT user_id FROM sessions WHERE session_id = ?",
                    (session_id,)
                ).fetchone()
                if row:
                    user_id = f"user_{row['user_id']}"
        except Exception:
            pass  # Don't break request on logging failure

    # Format: ISO timestamp | IP | METHOD /path | status | duration | user
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    log_line = f"{timestamp} | {client_ip} | {request.method} {request.url.path} | {response.status_code} | {duration_ms:.0f}ms | {user_id}"

    # Log at appropriate level based on status code
    if response.status_code >= 500:
        request_logger.error(log_line)
    elif response.status_code >= 400:
        request_logger.warning(log_line)
    else:
        request_logger.info(log_line)

    return response


# ============================================================================
# Tokenization
# ============================================================================

def get_tokenizer():
    """Get the global tokenizer instance."""
    global _tokenizer
    if _tokenizer is None:
        print(f"Loading tokenizer: {TOKENIZER_MODEL}")
        _tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_MODEL)
        print(f"Tokenizer loaded: {type(_tokenizer).__name__}")
    return _tokenizer


def tokenize_text(text: str) -> list[dict]:
    """
    Tokenize text using the target model's tokenizer.
    
    Returns tokens with character offsets that map back to the original text.
    WordPiece subword tokens (##xxx) are marked with is_subword=True.
    
    This ensures annotators see exactly what the model will see during training.
    """
    tokenizer = get_tokenizer()
    
    # Tokenize with offset mapping
    encoding = tokenizer(
        text,
        return_offsets_mapping=True,
        add_special_tokens=False,  # Don't add [CLS], [SEP] for display
    )
    
    tokens = encoding.tokens()
    offsets = encoding.offset_mapping
    
    words = []
    for i, (token, (char_start, char_end)) in enumerate(zip(tokens, offsets)):
        # Detect WordPiece subword tokens
        is_subword = token.startswith("##")
        display_text = token[2:] if is_subword else token  # Strip ## for display
        
        words.append({
            "index": i,
            "text": display_text,
            "token": token,  # Original token including ## prefix
            "char_start": char_start,
            "char_end": char_end,
            "is_subword": is_subword,
        })

    return words


# ============================================================================
# Tier 0: Automatic Label Computation
# ============================================================================

def compute_tier0_labels(premise: str, hypothesis: str, premise_tokens: list[dict], hypothesis_tokens: list[dict]) -> dict:
    """
    Compute Tier 0 automatic labels for an example.

    Returns dict mapping label_name -> {premise: [indices], hypothesis: [indices]}
    """
    result = {}

    # Get token texts (lowercase for matching)
    premise_texts = [t["text"].lower() for t in premise_tokens]
    hypothesis_texts = [t["text"].lower() for t in hypothesis_tokens]

    # Token alignment labels
    premise_set = set(premise_texts)
    hypothesis_set = set(hypothesis_texts)

    # aligned_tokens: tokens appearing in both
    result["aligned_tokens"] = {
        "premise": [i for i, t in enumerate(premise_texts) if t in hypothesis_set],
        "hypothesis": [i for i, t in enumerate(hypothesis_texts) if t in premise_set]
    }

    # premise_only: tokens only in premise
    result["premise_only"] = {
        "premise": [i for i, t in enumerate(premise_texts) if t not in hypothesis_set],
        "hypothesis": []
    }

    # hypothesis_only: tokens only in hypothesis
    result["hypothesis_only"] = {
        "premise": [],
        "hypothesis": [i for i, t in enumerate(hypothesis_texts) if t not in premise_set]
    }

    # Wordlist-based detection
    for label_name, wordlist in TIER0_WORDLISTS.items():
        premise_matches = []
        hypothesis_matches = []

        for i, token in enumerate(premise_texts):
            # Check exact match or if token contains any wordlist entry
            if token in wordlist:
                premise_matches.append(i)
            else:
                # Check for contractions and multi-word entries
                for word in wordlist:
                    if word in token or token.endswith(word):
                        premise_matches.append(i)
                        break

        for i, token in enumerate(hypothesis_texts):
            if token in wordlist:
                hypothesis_matches.append(i)
            else:
                for word in wordlist:
                    if word in token or token.endswith(word):
                        hypothesis_matches.append(i)
                        break

        result[label_name] = {
            "premise": premise_matches,
            "hypothesis": hypothesis_matches
        }

    return result


# ============================================================================
# Tier 1: WordNet-based Semantic Relation Detection
# ============================================================================

def get_wordnet_relations(word: str) -> dict:
    """
    Get all WordNet semantic relations for a word.

    Returns dict with sets of related words for each relation type.
    """
    relations = {
        "hypernyms": set(),    # More general terms (dog -> animal)
        "hyponyms": set(),     # More specific terms (animal -> dog)
        "synonyms": set(),     # Same meaning (big -> large)
        "antonyms": set(),     # Opposite meaning (hot -> cold)
        "meronyms": set(),     # Part-of relations (car -> wheel)
        "holonyms": set()      # Whole-of relations (wheel -> car)
    }

    if not ensure_wordnet_data():
        return relations

    try:
        from nltk.corpus import wordnet

        synsets = wordnet.synsets(word)
        for synset in synsets:
            # Synonyms: all lemmas in the synset
            for lemma in synset.lemmas():
                if lemma.name().lower() != word.lower():
                    relations["synonyms"].add(lemma.name().lower().replace("_", " "))

                # Antonyms: from lemma antonyms
                for antonym in lemma.antonyms():
                    relations["antonyms"].add(antonym.name().lower().replace("_", " "))

            # Hypernyms: more general concepts
            for hypernym in synset.hypernyms():
                for lemma in hypernym.lemmas():
                    relations["hypernyms"].add(lemma.name().lower().replace("_", " "))

            # Hyponyms: more specific concepts
            for hyponym in synset.hyponyms():
                for lemma in hyponym.lemmas():
                    relations["hyponyms"].add(lemma.name().lower().replace("_", " "))

            # Meronyms: parts of this concept
            for meronym in synset.part_meronyms() + synset.substance_meronyms() + synset.member_meronyms():
                for lemma in meronym.lemmas():
                    relations["meronyms"].add(lemma.name().lower().replace("_", " "))

            # Holonyms: wholes that contain this concept
            for holonym in synset.part_holonyms() + synset.substance_holonyms() + synset.member_holonyms():
                for lemma in holonym.lemmas():
                    relations["holonyms"].add(lemma.name().lower().replace("_", " "))

    except Exception as e:
        # Silently fail for individual words
        pass

    return relations


def compute_tier1_labels(premise: str, hypothesis: str, premise_tokens: list[dict], hypothesis_tokens: list[dict]) -> dict:
    """
    Compute Tier 1 weak supervision labels using WordNet semantic relations.

    Detects when tokens in hypothesis have semantic relations to premise tokens:
    - hypernym: hypothesis token is more general than premise token
    - hyponym: hypothesis token is more specific than premise token
    - synonym: hypothesis token has same meaning as premise token
    - antonym: hypothesis token has opposite meaning to premise token
    - meronym: hypothesis token is a part of something in premise
    - holonym: hypothesis token is a whole containing something in premise

    Returns dict mapping label_name -> {premise: [indices], hypothesis: [indices]}
    """
    result = {
        "hypernym": {"premise": [], "hypothesis": []},
        "hyponym": {"premise": [], "hypothesis": []},
        "synonym": {"premise": [], "hypothesis": []},
        "antonym": {"premise": [], "hypothesis": []},
        "meronym": {"premise": [], "hypothesis": []},
        "holonym": {"premise": [], "hypothesis": []}
    }

    if not ensure_wordnet_data():
        return result

    # Get token texts
    premise_texts = [t["text"].lower() for t in premise_tokens]
    hypothesis_texts = [t["text"].lower() for t in hypothesis_tokens]

    # Build WordNet relation maps for premise tokens
    premise_relations = {}
    for i, token in enumerate(premise_texts):
        # Skip very short tokens and punctuation
        if len(token) < 2 or not token.isalpha():
            continue
        premise_relations[i] = get_wordnet_relations(token)

    # Check each hypothesis token against premise relations
    for h_idx, h_token in enumerate(hypothesis_texts):
        if len(h_token) < 2 or not h_token.isalpha():
            continue

        h_token_lower = h_token.lower()

        for p_idx, p_relations in premise_relations.items():
            p_token = premise_texts[p_idx]

            # Skip if same token (already covered by Tier 0 alignment)
            if h_token_lower == p_token:
                continue

            # Check if hypothesis token appears in premise token's relations
            # hypernym: h is more general than p (p's hypernym contains h)
            if h_token_lower in p_relations["hypernyms"]:
                if p_idx not in result["hypernym"]["premise"]:
                    result["hypernym"]["premise"].append(p_idx)
                if h_idx not in result["hypernym"]["hypothesis"]:
                    result["hypernym"]["hypothesis"].append(h_idx)

            # hyponym: h is more specific than p (p's hyponym contains h)
            if h_token_lower in p_relations["hyponyms"]:
                if p_idx not in result["hyponym"]["premise"]:
                    result["hyponym"]["premise"].append(p_idx)
                if h_idx not in result["hyponym"]["hypothesis"]:
                    result["hyponym"]["hypothesis"].append(h_idx)

            # synonym: h has same meaning as p
            if h_token_lower in p_relations["synonyms"]:
                if p_idx not in result["synonym"]["premise"]:
                    result["synonym"]["premise"].append(p_idx)
                if h_idx not in result["synonym"]["hypothesis"]:
                    result["synonym"]["hypothesis"].append(h_idx)

            # antonym: h is opposite of p
            if h_token_lower in p_relations["antonyms"]:
                if p_idx not in result["antonym"]["premise"]:
                    result["antonym"]["premise"].append(p_idx)
                if h_idx not in result["antonym"]["hypothesis"]:
                    result["antonym"]["hypothesis"].append(h_idx)

            # meronym: h is a part of p (p's meronyms contains h)
            if h_token_lower in p_relations["meronyms"]:
                if p_idx not in result["meronym"]["premise"]:
                    result["meronym"]["premise"].append(p_idx)
                if h_idx not in result["meronym"]["hypothesis"]:
                    result["meronym"]["hypothesis"].append(h_idx)

            # holonym: h is a whole containing p (p's holonyms contains h)
            if h_token_lower in p_relations["holonyms"]:
                if p_idx not in result["holonym"]["premise"]:
                    result["holonym"]["premise"].append(p_idx)
                if h_idx not in result["holonym"]["hypothesis"]:
                    result["holonym"]["hypothesis"].append(h_idx)

    return result


def store_auto_spans(conn, example_id: str, auto_labels: dict, tier: int = 0):
    """Store computed auto-spans in the database."""
    for label_name, sources in auto_labels.items():
        for source, indices in sources.items():
            if indices:  # Only store non-empty spans
                conn.execute("""
                    INSERT OR REPLACE INTO auto_spans (example_id, tier, label_name, source, word_indices)
                    VALUES (?, ?, ?, ?, ?)
                """, (example_id, tier, label_name, source, json.dumps(indices)))


def get_auto_spans(conn, example_id: str) -> dict:
    """Retrieve computed auto-spans for an example."""
    rows = conn.execute("""
        SELECT tier, label_name, source, word_indices
        FROM auto_spans
        WHERE example_id = ?
    """, (example_id,)).fetchall()

    result = {"tier0": {}, "tier1": {}}
    for row in rows:
        tier_key = f"tier{row['tier']}"
        label = row["label_name"]
        source = row["source"]
        indices = json.loads(row["word_indices"])

        if label not in result[tier_key]:
            result[tier_key][label] = {"premise": [], "hypothesis": []}
        result[tier_key][label][source] = indices

    return result


def ensure_auto_spans_computed(conn, example_id: str, premise: str, hypothesis: str) -> dict:
    """Ensure auto-spans are computed for an example, computing if needed."""
    # Check if already computed (check both tiers)
    existing_tier0 = conn.execute(
        "SELECT example_id FROM auto_spans WHERE example_id = ? AND tier = 0 LIMIT 1",
        (example_id,)
    ).fetchone()
    existing_tier1 = conn.execute(
        "SELECT example_id FROM auto_spans WHERE example_id = ? AND tier = 1 LIMIT 1",
        (example_id,)
    ).fetchone()

    # Tokenize once for both tiers
    premise_tokens = tokenize_text(premise)
    hypothesis_tokens = tokenize_text(hypothesis)

    # Compute Tier 0 if not cached
    if not existing_tier0:
        tier0_labels = compute_tier0_labels(premise, hypothesis, premise_tokens, hypothesis_tokens)
        store_auto_spans(conn, example_id, tier0_labels, tier=0)

    # Compute Tier 1 if not cached (WordNet semantic relations)
    if not existing_tier1:
        tier1_labels = compute_tier1_labels(premise, hypothesis, premise_tokens, hypothesis_tokens)
        store_auto_spans(conn, example_id, tier1_labels, tier=1)

    if not existing_tier0 or not existing_tier1:
        conn.commit()

    return get_auto_spans(conn, example_id)


# ============================================================================
# Database Setup
# ============================================================================

def init_db():
    """Initialize SQLite database with user support."""
    with get_db() as conn:
        # Check if we need to migrate (do users table exist?)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )
        users_exist = cursor.fetchone() is not None
        
        # Check if labels table exists (for migration)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='labels'"
        )
        labels_exist = cursor.fetchone() is not None

        # Check if annotator_agreement table exists (for reliability migration)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='annotator_agreement'"
        )
        annotator_agreement_exists = cursor.fetchone() is not None

        # Check if annotator_id column exists in labels
        needs_user_migration = False
        needs_custom_migration = False
        needs_role_migration = False
        if labels_exist:
            cursor = conn.execute("PRAGMA table_info(labels)")
            columns = [row[1] for row in cursor.fetchall()]
            needs_user_migration = 'annotator_id' not in columns
            needs_custom_migration = 'is_custom' not in columns

        # Check if reliability columns exist
        needs_reliability_migration = False
        if annotator_agreement_exists:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(annotator_agreement)").fetchall()]
            needs_reliability_migration = 'reliability_score' not in columns

        
        # Check if role column exists in users
        if users_exist:
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in cursor.fetchall()]
            needs_role_migration = 'role' not in columns

        # Check if examples table needs pool_status column
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='examples'"
        )
        examples_exist = cursor.fetchone() is not None
        needs_pool_migration = False
        if examples_exist:
            cursor = conn.execute("PRAGMA table_info(examples)")
            columns = [row[1] for row in cursor.fetchall()]
            needs_pool_migration = 'pool_status' not in columns

        # Check if users table needs training columns
        needs_training_migration = False
        if users_exist:
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in cursor.fetchall()]
            needs_training_migration = 'training_required' not in columns

        # Create users table first (needed for foreign keys)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                display_name TEXT,
                role TEXT NOT NULL DEFAULT 'annotator',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)

        # Handle role migration for existing databases
        if needs_role_migration:
            migrate_add_role(conn)

        # Create system users if they don't exist
        conn.execute("""
            INSERT OR IGNORE INTO users (id, username, display_name, role)
            VALUES (?, 'anonymous', 'Anonymous User', 'annotator')
        """, (ANONYMOUS_USER_ID,))
        
        conn.execute("""
            INSERT OR IGNORE INTO users (id, username, display_name, role)
            VALUES (?, 'legacy', 'Legacy Annotations', 'annotator')
        """, (LEGACY_USER_ID,))

        # Bootstrap admin user if configured
        if ADMIN_USER:
            bootstrap_admin_user(conn)

        # Create examples table
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS examples (
                id TEXT PRIMARY KEY,
                dataset TEXT NOT NULL,
                premise TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                gold_label INTEGER,
                gold_label_text TEXT,
                source_file TEXT,
                pool_status TEXT DEFAULT 'zero_entry',
                loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_examples_dataset ON examples(dataset);
            CREATE INDEX IF NOT EXISTS idx_examples_pool ON examples(pool_status);
        """)

        # Add pool_status to existing examples table if needed
        if needs_pool_migration:
            migrate_add_pool_status(conn)

        # Create example_locks table for concurrency handling
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS example_locks (
                example_id TEXT PRIMARY KEY,
                locked_by INTEGER NOT NULL,
                locked_until TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE,
                FOREIGN KEY (locked_by) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_locks_user ON example_locks(locked_by);
            CREATE INDEX IF NOT EXISTS idx_locks_until ON example_locks(locked_until);
        """)

        # Create agreement tracking tables
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS example_agreement (
                example_id TEXT PRIMARY KEY,
                annotation_count INTEGER DEFAULT 0,
                agreement_score REAL,
                complexity_agreement REAL,
                span_agreement REAL,
                last_calculated TIMESTAMP,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS annotator_agreement (
                user_id INTEGER PRIMARY KEY,
                total_annotations INTEGER DEFAULT 0,
                agreement_with_consensus REAL,
                complexity_agreement REAL,
                span_agreement REAL,
                reliability_score REAL,
                calibration_status TEXT DEFAULT 'provisional',
                scored_annotation_count INTEGER DEFAULT 0,
                last_calculated TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_example_agreement_score ON example_agreement(agreement_score);
            CREATE INDEX IF NOT EXISTS idx_annotator_agreement_score ON annotator_agreement(agreement_with_consensus);
            CREATE INDEX IF NOT EXISTS idx_annotator_reliability ON annotator_agreement(reliability_score);
        """)

        # Add reliability columns to existing annotator_agreement table if needed
        if needs_reliability_migration:
            migrate_add_reliability_columns(conn)

        # Handle migration for existing tables
        if needs_user_migration and labels_exist:
            # Migrate existing data (add annotator_id)
            migrate_existing_data(conn)
        elif needs_custom_migration and labels_exist:
            # Add is_custom column to existing table
            migrate_add_is_custom(conn)
        else:
            # Create new tables with all columns
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    example_id TEXT NOT NULL,
                    annotator_id INTEGER NOT NULL DEFAULT 1,
                    label_name TEXT NOT NULL,
                    label_color TEXT,
                    is_custom BOOLEAN NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (example_id) REFERENCES examples(id),
                    FOREIGN KEY (annotator_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS span_selections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    word_index INTEGER NOT NULL,
                    word_text TEXT NOT NULL,
                    char_start INTEGER,
                    char_end INTEGER,
                    FOREIGN KEY (label_id) REFERENCES labels(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS complexity_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    example_id TEXT NOT NULL,
                    annotator_id INTEGER NOT NULL DEFAULT 1,
                    reasoning INTEGER,
                    creativity INTEGER,
                    domain_knowledge INTEGER,
                    contextual INTEGER,
                    constraints INTEGER,
                    ambiguity INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (example_id) REFERENCES examples(id),
                    FOREIGN KEY (annotator_id) REFERENCES users(id),
                    UNIQUE(example_id, annotator_id)
                );

                CREATE TABLE IF NOT EXISTS skipped (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    example_id TEXT NOT NULL,
                    annotator_id INTEGER NOT NULL DEFAULT 1,
                    skipped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (example_id) REFERENCES examples(id),
                    FOREIGN KEY (annotator_id) REFERENCES users(id),
                    UNIQUE(example_id, annotator_id)
                );

                CREATE TABLE IF NOT EXISTS flagged_examples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    example_id TEXT NOT NULL,
                    flagged_by INTEGER,
                    flag_source TEXT NOT NULL DEFAULT 'manual',
                    flag_reason TEXT,
                    agreement_score REAL,
                    flagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT NOT NULL DEFAULT 'pending',
                    resolution TEXT,
                    resolved_by INTEGER,
                    resolved_at TIMESTAMP,
                    FOREIGN KEY (example_id) REFERENCES examples(id),
                    FOREIGN KEY (flagged_by) REFERENCES users(id),
                    FOREIGN KEY (resolved_by) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_labels_example ON labels(example_id);
                CREATE INDEX IF NOT EXISTS idx_labels_annotator ON labels(annotator_id);
                CREATE INDEX IF NOT EXISTS idx_labels_custom ON labels(is_custom);
                CREATE INDEX IF NOT EXISTS idx_spans_label ON span_selections(label_id);
                CREATE INDEX IF NOT EXISTS idx_complexity_annotator ON complexity_scores(annotator_id);
                CREATE INDEX IF NOT EXISTS idx_skipped_annotator ON skipped(annotator_id);
                CREATE INDEX IF NOT EXISTS idx_flagged_example ON flagged_examples(example_id);
                CREATE INDEX IF NOT EXISTS idx_flagged_status ON flagged_examples(status);
            """)

        # Create gold annotations table for training mode
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS gold_annotations (
                example_id TEXT PRIMARY KEY,
                gold_complexity_scores TEXT NOT NULL,
                gold_labels TEXT,
                explanation TEXT,
                created_by INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE,
                FOREIGN KEY (created_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS training_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                example_id TEXT NOT NULL,
                submitted_complexity_scores TEXT NOT NULL,
                submitted_labels TEXT,
                accuracy_score REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE,
                UNIQUE(user_id, example_id)
            );

            CREATE INDEX IF NOT EXISTS idx_gold_example ON gold_annotations(example_id);
            CREATE INDEX IF NOT EXISTS idx_training_user ON training_submissions(user_id);
        """)

        # Create auto_spans table for tiered annotation system (Tier 0 + Tier 1)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS auto_spans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                example_id TEXT NOT NULL,
                tier INTEGER NOT NULL,
                label_name TEXT NOT NULL,
                source TEXT NOT NULL,
                word_indices TEXT NOT NULL,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE,
                UNIQUE(example_id, label_name, source)
            );

            CREATE INDEX IF NOT EXISTS idx_auto_spans_example ON auto_spans(example_id);
            CREATE INDEX IF NOT EXISTS idx_auto_spans_tier ON auto_spans(tier);

            CREATE TABLE IF NOT EXISTS tier1_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                example_id TEXT NOT NULL,
                label_name TEXT NOT NULL,
                verified_by INTEGER,
                is_correct BOOLEAN,
                verified_at TIMESTAMP,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE,
                FOREIGN KEY (verified_by) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_tier1_quality_label ON tier1_quality(label_name);
        """)

        # Add training columns to users table if needed
        if needs_training_migration:
            migrate_add_training_columns(conn)


def migrate_add_role(conn):
    """Add role column to existing users table."""
    print("Adding role column to users table...")
    
    # Add column with default value
    conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'annotator'")
    
    print("Role column added. All existing users set to 'annotator' role.")


def bootstrap_admin_user(conn):
    """Bootstrap the configured admin user."""
    if not ADMIN_USER:
        return
    
    # Check if user exists
    row = conn.execute(
        "SELECT id, role FROM users WHERE username = ?",
        (ADMIN_USER,)
    ).fetchone()
    
    if row:
        # User exists - update role to admin if not already
        if row["role"] != ROLE_ADMIN:
            conn.execute(
                "UPDATE users SET role = ? WHERE id = ?",
                (ROLE_ADMIN, row["id"])
            )
            print(f"User '{ADMIN_USER}' promoted to admin role.")
        else:
            print(f"User '{ADMIN_USER}' already has admin role.")
    else:
        # User doesn't exist yet - they'll get admin role when they register
        print(f"Admin user '{ADMIN_USER}' not found. Will be granted admin role on registration.")


def migrate_existing_data(conn):
    """Migrate existing tables to include annotator_id."""
    print("Migrating existing database to multi-user schema...")
    
    # Rename old tables
    conn.executescript("""
        ALTER TABLE labels RENAME TO labels_old;
        ALTER TABLE complexity_scores RENAME TO complexity_scores_old;
        ALTER TABLE skipped RENAME TO skipped_old;
    """)
    
    # Create new tables with annotator_id and is_custom
    conn.executescript("""
        CREATE TABLE labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            example_id TEXT NOT NULL,
            annotator_id INTEGER NOT NULL DEFAULT 2,
            label_name TEXT NOT NULL,
            label_color TEXT,
            is_custom BOOLEAN NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (example_id) REFERENCES examples(id),
            FOREIGN KEY (annotator_id) REFERENCES users(id)
        );

        CREATE TABLE complexity_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            example_id TEXT NOT NULL,
            annotator_id INTEGER NOT NULL DEFAULT 2,
            reasoning INTEGER,
            creativity INTEGER,
            domain_knowledge INTEGER,
            contextual INTEGER,
            constraints INTEGER,
            ambiguity INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (example_id) REFERENCES examples(id),
            FOREIGN KEY (annotator_id) REFERENCES users(id),
            UNIQUE(example_id, annotator_id)
        );

        CREATE TABLE skipped (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            example_id TEXT NOT NULL,
            annotator_id INTEGER NOT NULL DEFAULT 2,
            skipped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (example_id) REFERENCES examples(id),
            FOREIGN KEY (annotator_id) REFERENCES users(id),
            UNIQUE(example_id, annotator_id)
        );
    """)
    
    # Copy data with legacy user ID, determining is_custom for each label
    old_labels = conn.execute("SELECT * FROM labels_old").fetchall()
    for label in old_labels:
        is_custom = 0 if is_system_label(label["label_name"]) else 1
        conn.execute("""
            INSERT INTO labels (id, example_id, annotator_id, label_name, label_color, is_custom, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (label["id"], label["example_id"], LEGACY_USER_ID, 
              label["label_name"], label["label_color"], is_custom, label["created_at"]))
    
    conn.execute("""
        INSERT INTO complexity_scores (example_id, annotator_id, reasoning, creativity, 
                                       domain_knowledge, contextual, constraints, ambiguity, created_at)
        SELECT example_id, ?, reasoning, creativity, domain_knowledge, contextual, 
               constraints, ambiguity, created_at
        FROM complexity_scores_old
    """, (LEGACY_USER_ID,))
    
    conn.execute("""
        INSERT INTO skipped (example_id, annotator_id, skipped_at)
        SELECT example_id, ?, skipped_at
        FROM skipped_old
    """, (LEGACY_USER_ID,))
    
    # Drop old tables
    conn.executescript("""
        DROP TABLE labels_old;
        DROP TABLE complexity_scores_old;
        DROP TABLE skipped_old;
    """)
    
    # Recreate indexes
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_labels_example ON labels(example_id);
        CREATE INDEX IF NOT EXISTS idx_labels_annotator ON labels(annotator_id);
        CREATE INDEX IF NOT EXISTS idx_labels_custom ON labels(is_custom);
        CREATE INDEX IF NOT EXISTS idx_spans_label ON span_selections(label_id);
        CREATE INDEX IF NOT EXISTS idx_complexity_annotator ON complexity_scores(annotator_id);
        CREATE INDEX IF NOT EXISTS idx_skipped_annotator ON skipped(annotator_id);
    """)
    
    print("Migration complete. Existing annotations assigned to 'legacy' user.")


def migrate_add_is_custom(conn):
    """Add is_custom column to existing labels table."""
    print("Adding is_custom column to labels table...")
    
    # Add column
    conn.execute("ALTER TABLE labels ADD COLUMN is_custom BOOLEAN NOT NULL DEFAULT 0")
    
    # Update existing labels based on SYSTEM_LABELS
    # Mark labels not in system labels as custom
    all_labels = conn.execute("SELECT id, label_name FROM labels").fetchall()
    for label in all_labels:
        is_custom = 0 if is_system_label(label["label_name"]) else 1
        conn.execute("UPDATE labels SET is_custom = ? WHERE id = ?", (is_custom, label["id"]))
    
    # Add index
    conn.execute("CREATE INDEX IF NOT EXISTS idx_labels_custom ON labels(is_custom)")
    
    print(f"Migration complete. Updated {len(all_labels)} labels.")


def migrate_add_pool_status(conn):
    """Add pool_status column to existing examples table."""
    print("Adding pool_status column to examples table...")
    
    # Add column with default value
    conn.execute("ALTER TABLE examples ADD COLUMN pool_status TEXT DEFAULT 'zero_entry'")
    
    # Update existing examples based on annotation count
    # Examples with annotations go to 'building' pool
    conn.execute("""
        UPDATE examples SET pool_status = 'building'
        WHERE id IN (SELECT DISTINCT example_id FROM complexity_scores)
    """)
    
    # Add index
    conn.execute("CREATE INDEX IF NOT EXISTS idx_examples_pool ON examples(pool_status)")
    
    print("Migration complete. Existing annotated examples moved to \'building\' pool.")

def migrate_add_reliability_columns(conn):
    """Add reliability scoring columns to existing annotator_agreement table."""
    print("Adding reliability scoring columns to annotator_agreement table...")

    # Add new columns
    conn.execute("ALTER TABLE annotator_agreement ADD COLUMN reliability_score REAL")
    conn.execute("ALTER TABLE annotator_agreement ADD COLUMN calibration_status TEXT DEFAULT 'provisional'")
    conn.execute("ALTER TABLE annotator_agreement ADD COLUMN scored_annotation_count INTEGER DEFAULT 0")

    # Add index for reliability score
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annotator_reliability ON annotator_agreement(reliability_score)")

    # Initialize scored_annotation_count from existing data
    conn.execute("""
        UPDATE annotator_agreement SET scored_annotation_count = total_annotations
        WHERE total_annotations > 0
    """)

    # Calculate initial reliability scores for existing annotators
    # (reliability_score = agreement_with_consensus * 100, since it's already 0-1)
    conn.execute("""
        UPDATE annotator_agreement
        SET reliability_score = agreement_with_consensus * 100,
            calibration_status = CASE
                WHEN total_annotations >= ? THEN 'calibrated'
                ELSE 'provisional'
            END
        WHERE agreement_with_consensus IS NOT NULL
    """, (CALIBRATION_MIN_SCORED,))

    print("Migration complete. Reliability columns added and initialized.")


def migrate_add_training_columns(conn):
    """Add training columns to existing users table."""
    print("Adding training columns to users table...")

    conn.execute("ALTER TABLE users ADD COLUMN training_required BOOLEAN DEFAULT 1")
    conn.execute("ALTER TABLE users ADD COLUMN training_completed_at TIMESTAMP")

    # Mark existing users as not requiring training (they're already active)
    conn.execute("""
        UPDATE users SET training_required = 0
        WHERE id NOT IN (?, ?)
    """, (ANONYMOUS_USER_ID, LEGACY_USER_ID))

    print("Migration complete. Existing users marked as training complete.")


@contextmanager
def get_db():
    """Database connection context manager."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# Agreement Calculation Helpers
# ============================================================================

def calculate_complexity_agreement(scores: list[dict]) -> float:
    """
    Calculate agreement for complexity scores across annotators.
    
    Uses normalized mean absolute deviation - lower deviation = higher agreement.
    Returns a score from 0 (no agreement) to 1 (perfect agreement).
    """
    if len(scores) < 2:
        return None
    
    dimensions = ['reasoning', 'creativity', 'domain_knowledge', 'contextual', 'constraints', 'ambiguity']
    total_agreement = 0
    valid_dimensions = 0
    
    for dim in dimensions:
        values = [s[dim] for s in scores if s.get(dim) is not None]
        if len(values) < 2:
            continue
        
        # Calculate mean and mean absolute deviation
        mean_val = sum(values) / len(values)
        mad = sum(abs(v - mean_val) for v in values) / len(values)
        
        # Normalize to 0-1 scale (max deviation is 5 on a 0-10 scale)
        # Agreement = 1 - (MAD / 5)
        dimension_agreement = max(0, 1 - (mad / 5))
        total_agreement += dimension_agreement
        valid_dimensions += 1
    
    if valid_dimensions == 0:
        return None
    
    return total_agreement / valid_dimensions


def calculate_span_agreement(annotations: list[dict]) -> float:
    """
    Calculate agreement for span selections across annotators.
    
    Uses Jaccard similarity - intersection over union of selected spans.
    Returns a score from 0 (no overlap) to 1 (perfect agreement).
    """
    if len(annotations) < 2:
        return None
    
    # Extract span sets per annotator, per label
    # Format: {annotator_id: {label_name: set((source, word_index), ...)}}
    annotator_spans = defaultdict(lambda: defaultdict(set))
    
    for ann in annotations:
        annotator_id = ann['annotator_id']
        for label in ann.get('labels', []):
            label_name = label['label_name']
            for span in label.get('spans', []):
                annotator_spans[annotator_id][label_name].add(
                    (span['source'], span['word_index'])
                )
    
    if len(annotator_spans) < 2:
        return None
    
    # Calculate pairwise Jaccard similarity for each label
    annotator_ids = list(annotator_spans.keys())
    all_labels = set()
    for spans in annotator_spans.values():
        all_labels.update(spans.keys())
    
    if not all_labels:
        return 1.0  # No spans = perfect agreement (both empty)
    
    total_similarity = 0
    comparisons = 0
    
    for i, aid1 in enumerate(annotator_ids):
        for aid2 in annotator_ids[i+1:]:
            label_similarities = []
            for label in all_labels:
                set1 = annotator_spans[aid1].get(label, set())
                set2 = annotator_spans[aid2].get(label, set())
                
                if not set1 and not set2:
                    # Both empty = perfect agreement for this label
                    label_similarities.append(1.0)
                elif not set1 or not set2:
                    # One empty, one not = no agreement
                    label_similarities.append(0.0)
                else:
                    # Jaccard: |intersection| / |union|
                    intersection = len(set1 & set2)
                    union = len(set1 | set2)
                    label_similarities.append(intersection / union)
            
            if label_similarities:
                total_similarity += sum(label_similarities) / len(label_similarities)
                comparisons += 1
    
    if comparisons == 0:
        return None
    
    return total_similarity / comparisons


def calculate_example_agreement(conn, example_id: str) -> dict:
    """
    Calculate comprehensive agreement metrics for an example.
    
    Returns dict with:
    - annotation_count: number of annotators
    - agreement_score: combined agreement (0-1)
    - complexity_agreement: agreement on complexity scores
    - span_agreement: agreement on span selections
    """
    # Get all complexity scores for this example
    scores = conn.execute("""
        SELECT annotator_id, reasoning, creativity, domain_knowledge,
               contextual, constraints, ambiguity
        FROM complexity_scores
        WHERE example_id = ?
    """, (example_id,)).fetchall()
    
    scores = [dict(s) for s in scores]
    annotation_count = len(scores)
    
    if annotation_count < 2:
        return {
            'annotation_count': annotation_count,
            'agreement_score': None,
            'complexity_agreement': None,
            'span_agreement': None,
        }
    
    # Calculate complexity agreement
    complexity_agreement = calculate_complexity_agreement(scores)
    
    # Get all span annotations for this example
    annotations = []
    for score in scores:
        annotator_id = score['annotator_id']
        labels = conn.execute("""
            SELECT l.label_name, l.label_color,
                   s.source, s.word_index, s.word_text
            FROM labels l
            LEFT JOIN span_selections s ON s.label_id = l.id
            WHERE l.example_id = ? AND l.annotator_id = ?
        """, (example_id, annotator_id)).fetchall()
        
        # Group by label
        label_dict = defaultdict(lambda: {'label_name': '', 'spans': []})
        for row in labels:
            label_name = row['label_name']
            label_dict[label_name]['label_name'] = label_name
            if row['source']:  # Has span data
                label_dict[label_name]['spans'].append({
                    'source': row['source'],
                    'word_index': row['word_index'],
                })
        
        annotations.append({
            'annotator_id': annotator_id,
            'labels': list(label_dict.values())
        })
    
    # Calculate span agreement
    span_agreement = calculate_span_agreement(annotations)
    
    # Combined agreement: weighted average (complexity more important for NLI)
    weights = {'complexity': 0.6, 'span': 0.4}
    
    combined_parts = []
    if complexity_agreement is not None:
        combined_parts.append(('complexity', complexity_agreement))
    if span_agreement is not None:
        combined_parts.append(('span', span_agreement))
    
    if combined_parts:
        total_weight = sum(weights[p[0]] for p in combined_parts)
        agreement_score = sum(weights[p[0]] * p[1] for p in combined_parts) / total_weight
    else:
        agreement_score = None
    
    return {
        'annotation_count': annotation_count,
        'agreement_score': agreement_score,
        'complexity_agreement': complexity_agreement,
        'span_agreement': span_agreement,
    }


def update_example_agreement(conn, example_id: str):
    """
    Recalculate and store agreement metrics for an example.
    Also updates pool status based on agreement.
    """
    metrics = calculate_example_agreement(conn, example_id)
    
    # Store in example_agreement table
    conn.execute("""
        INSERT INTO example_agreement (example_id, annotation_count, agreement_score,
                                       complexity_agreement, span_agreement, last_calculated)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(example_id) DO UPDATE SET
            annotation_count = excluded.annotation_count,
            agreement_score = excluded.agreement_score,
            complexity_agreement = excluded.complexity_agreement,
            span_agreement = excluded.span_agreement,
            last_calculated = CURRENT_TIMESTAMP
    """, (example_id, metrics['annotation_count'], metrics['agreement_score'],
          metrics['complexity_agreement'], metrics['span_agreement']))

    # Update pool status
    update_example_pool_status(conn, example_id, metrics)

    # Auto-flag if agreement is below controversial threshold
    if metrics['agreement_score'] is not None and metrics['annotation_count'] >= CONSENSUS_THRESHOLD:
        auto_flag_controversial(conn, example_id, metrics['agreement_score'])

    return metrics


def update_example_pool_status(conn, example_id: str, metrics: dict):
    """
    Update pool status based on annotation count and agreement.
    
    Pool transitions:
    - zero_entry -> building: First annotation
    - building -> test: Reaches threshold AND high agreement
    - test -> building: If agreement drops (shouldn't happen often)
    """
    annotation_count = metrics['annotation_count']
    agreement_score = metrics['agreement_score']
    
    if annotation_count == 0:
        new_status = 'zero_entry'
    elif annotation_count < CONSENSUS_THRESHOLD:
        new_status = 'building'
    elif agreement_score is not None and agreement_score >= AGREEMENT_HIGH_THRESHOLD:
        new_status = 'test'
    else:
        # Has enough annotations but agreement is low or unknown
        new_status = 'building'
    
    conn.execute("""
        UPDATE examples SET pool_status = ? WHERE id = ?
    """, (new_status, example_id))


def calculate_annotator_agreement(conn, user_id: int) -> dict:
    """
    Calculate agreement metrics for a specific annotator.
    
    Measures how often they agree with consensus on examples
    that have reached the consensus threshold.
    """
    # Get all examples this user has annotated that have consensus
    user_examples = conn.execute("""
        SELECT c.example_id, ea.agreement_score, ea.annotation_count
        FROM complexity_scores c
        JOIN example_agreement ea ON c.example_id = ea.example_id
        WHERE c.annotator_id = ? AND ea.annotation_count >= ?
    """, (user_id, CONSENSUS_THRESHOLD)).fetchall()
    
    if not user_examples:
        return {
            'total_annotations': 0,
            'agreement_with_consensus': None,
            'complexity_agreement': None,
            'span_agreement': None,
        }
    
    total_annotations = len(user_examples)
    
    # For each example, compare this user's annotation to the consensus
    complexity_agreements = []
    span_agreements = []
    
    for row in user_examples:
        example_id = row['example_id']
        
        # Get this user's scores
        user_scores = conn.execute("""
            SELECT reasoning, creativity, domain_knowledge,
                   contextual, constraints, ambiguity
            FROM complexity_scores
            WHERE example_id = ? AND annotator_id = ?
        """, (example_id, user_id)).fetchone()
        
        if not user_scores:
            continue
        
        # Get average scores (consensus) from other annotators
        other_scores = conn.execute("""
            SELECT AVG(reasoning) as reasoning, AVG(creativity) as creativity,
                   AVG(domain_knowledge) as domain_knowledge, AVG(contextual) as contextual,
                   AVG(constraints) as constraints, AVG(ambiguity) as ambiguity
            FROM complexity_scores
            WHERE example_id = ? AND annotator_id != ?
        """, (example_id, user_id)).fetchone()
        
        if other_scores and other_scores['reasoning'] is not None:
            # Calculate agreement with consensus for complexity
            dimensions = ['reasoning', 'creativity', 'domain_knowledge', 'contextual', 'constraints', 'ambiguity']
            dim_agreements = []
            for dim in dimensions:
                user_val = user_scores[dim]
                consensus_val = other_scores[dim]
                if user_val is not None and consensus_val is not None:
                    # Agreement = 1 - normalized difference
                    diff = abs(user_val - consensus_val) / 100
                    dim_agreements.append(1 - diff)
            
            if dim_agreements:
                complexity_agreements.append(sum(dim_agreements) / len(dim_agreements))
        
        # Get this user's span selections
        user_spans = conn.execute("""
            SELECT l.label_name, s.source, s.word_index
            FROM labels l
            LEFT JOIN span_selections s ON s.label_id = l.id
            WHERE l.example_id = ? AND l.annotator_id = ?
        """, (example_id, user_id)).fetchall()
        
        # Get other annotators' span selections
        other_spans = conn.execute("""
            SELECT l.label_name, s.source, s.word_index, l.annotator_id
            FROM labels l
            LEFT JOIN span_selections s ON s.label_id = l.id
            WHERE l.example_id = ? AND l.annotator_id != ?
        """, (example_id, user_id)).fetchall()
        
        if other_spans:
            # Build user's span set
            user_span_set = defaultdict(set)
            for row in user_spans:
                if row['source']:
                    user_span_set[row['label_name']].add((row['source'], row['word_index']))
            
            # Build consensus span set (majority vote)
            span_votes = defaultdict(lambda: defaultdict(int))
            other_annotator_count = len(set(r['annotator_id'] for r in other_spans))
            
            for row in other_spans:
                if row['source']:
                    span_votes[row['label_name']][(row['source'], row['word_index'])] += 1
            
            # Consensus = spans selected by majority
            consensus_spans = defaultdict(set)
            majority_threshold = other_annotator_count / 2
            for label, spans in span_votes.items():
                for span, count in spans.items():
                    if count >= majority_threshold:
                        consensus_spans[label].add(span)
            
            # Calculate Jaccard with consensus
            all_labels = set(user_span_set.keys()) | set(consensus_spans.keys())
            if all_labels:
                label_agreements = []
                for label in all_labels:
                    user_set = user_span_set.get(label, set())
                    consensus_set = consensus_spans.get(label, set())
                    
                    if not user_set and not consensus_set:
                        label_agreements.append(1.0)
                    elif not user_set or not consensus_set:
                        label_agreements.append(0.0)
                    else:
                        intersection = len(user_set & consensus_set)
                        union = len(user_set | consensus_set)
                        label_agreements.append(intersection / union)
                
                span_agreements.append(sum(label_agreements) / len(label_agreements))
    
    # Aggregate results
    complexity_agreement = sum(complexity_agreements) / len(complexity_agreements) if complexity_agreements else None
    span_agreement = sum(span_agreements) / len(span_agreements) if span_agreements else None
    
    # Combined agreement
    if complexity_agreement is not None and span_agreement is not None:
        agreement_with_consensus = 0.6 * complexity_agreement + 0.4 * span_agreement
    elif complexity_agreement is not None:
        agreement_with_consensus = complexity_agreement
    elif span_agreement is not None:
        agreement_with_consensus = span_agreement
    else:
        agreement_with_consensus = None
    
    return {
        'total_annotations': total_annotations,
        'agreement_with_consensus': agreement_with_consensus,
        'complexity_agreement': complexity_agreement,
        'span_agreement': span_agreement,
    }


def update_annotator_agreement(conn, user_id: int):
    """Recalculate and store agreement metrics and reliability score for an annotator."""
    metrics = calculate_annotator_agreement(conn, user_id)

    # Calculate reliability score (0-100 scale)
    # Based on agreement_with_consensus which is already 0-1
    reliability_score = None
    if metrics['agreement_with_consensus'] is not None:
        reliability_score = metrics['agreement_with_consensus'] * 100

    # Determine calibration status
    scored_count = metrics['total_annotations']  # Annotations on examples with consensus
    calibration_status = 'calibrated' if scored_count >= CALIBRATION_MIN_SCORED else 'provisional'

    conn.execute("""
        INSERT INTO annotator_agreement (user_id, total_annotations, agreement_with_consensus,
                                         complexity_agreement, span_agreement, reliability_score,
                                         calibration_status, scored_annotation_count, last_calculated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            total_annotations = excluded.total_annotations,
            agreement_with_consensus = excluded.agreement_with_consensus,
            complexity_agreement = excluded.complexity_agreement,
            span_agreement = excluded.span_agreement,
            reliability_score = excluded.reliability_score,
            calibration_status = excluded.calibration_status,
            scored_annotation_count = excluded.scored_annotation_count,
            last_calculated = CURRENT_TIMESTAMP
    """, (user_id, metrics['total_annotations'], metrics['agreement_with_consensus'],
          metrics['complexity_agreement'], metrics['span_agreement'],
          reliability_score, calibration_status, scored_count))

    return metrics


# ============================================================================
# Locking Helpers
# ============================================================================

def acquire_lock(conn, example_id: str, user_id: int) -> Optional[datetime]:
    """
    Acquire a lock on an example for a user.
    
    Returns the lock expiry time if successful, None if already locked by another user.
    If already locked by the same user, extends the lock.
    """
    now = datetime.now()
    lock_until = now + timedelta(minutes=LOCK_TIMEOUT_MINUTES)
    
    # Check existing lock
    existing = conn.execute("""
        SELECT locked_by, locked_until FROM example_locks WHERE example_id = ?
    """, (example_id,)).fetchone()
    
    if existing:
        # Lock exists - check if it's expired or owned by this user
        existing_until = datetime.fromisoformat(existing["locked_until"])
        if existing["locked_by"] == user_id:
            # User's own lock - extend it
            conn.execute("""
                UPDATE example_locks SET locked_until = ? WHERE example_id = ?
            """, (lock_until.isoformat(), example_id))
            return lock_until
        elif existing_until > now:
            # Someone else's active lock
            return None
        else:
            # Expired lock - take it over
            conn.execute("""
                UPDATE example_locks SET locked_by = ?, locked_until = ?, created_at = ?
                WHERE example_id = ?
            """, (user_id, lock_until.isoformat(), now.isoformat(), example_id))
            return lock_until
    else:
        # No lock - create one
        conn.execute("""
            INSERT INTO example_locks (example_id, locked_by, locked_until)
            VALUES (?, ?, ?)
        """, (example_id, user_id, lock_until.isoformat()))
        return lock_until


def release_lock(conn, example_id: str, user_id: int) -> bool:
    """
    Release a lock on an example.
    
    Returns True if lock was released, False if user didn't own the lock.
    """
    result = conn.execute("""
        DELETE FROM example_locks WHERE example_id = ? AND locked_by = ?
    """, (example_id, user_id))
    return result.rowcount > 0


def get_lock_status(conn, example_id: str) -> Optional[dict]:
    """Get the current lock status for an example."""
    row = conn.execute("""
        SELECT l.locked_by, l.locked_until, u.username, u.display_name
        FROM example_locks l
        JOIN users u ON l.locked_by = u.id
        WHERE l.example_id = ?
    """, (example_id,)).fetchone()
    
    if not row:
        return None
    
    locked_until = datetime.fromisoformat(row["locked_until"])
    now = datetime.now()
    
    if locked_until <= now:
        # Expired - clean it up
        conn.execute("DELETE FROM example_locks WHERE example_id = ?", (example_id,))
        return None
    
    return {
        "locked_by": row["locked_by"],
        "locked_by_username": row["username"],
        "locked_by_display_name": row["display_name"],
        "locked_until": row["locked_until"],
        "expires_in_seconds": int((locked_until - now).total_seconds())
    }


def get_user_calibration_status(conn, user_id: int) -> tuple[str, float]:
    """
    Get user's calibration status and determine test question ratio.

    Returns:
        (calibration_status, test_ratio)
        - calibration_status: 'provisional' or 'calibrated'
        - test_ratio: probability of serving a test question (0.0 to 1.0)
    """
    if not CALIBRATION_ROUTING_ENABLED:
        return ('disabled', 0.0)

    # Check user's calibration status
    result = conn.execute("""
        SELECT calibration_status, scored_annotation_count
        FROM annotator_agreement WHERE user_id = ?
    """, (user_id,)).fetchone()

    if result is None or result['calibration_status'] == 'provisional':
        # New or provisional user - high calibration ratio
        return ('provisional', CALIBRATION_TEST_RATIO_NEW)
    else:
        # Calibrated user - occasional test questions for drift detection
        return ('calibrated', CALIBRATION_TEST_RATIO_ESTABLISHED)


def should_serve_test_question(test_ratio: float) -> bool:
    """Random roll to decide if we should serve a test question."""
    return random.random() < test_ratio


def cleanup_expired_locks(conn):
    """Remove all expired locks."""
    now = datetime.now().isoformat()
    result = conn.execute("""
        DELETE FROM example_locks WHERE locked_until < ?
    """, (now,))
    return result.rowcount


# ============================================================================
# Authentication Helpers
# ============================================================================

def hash_password(password: str) -> str:
    """Hash a password using SHA-256 with salt."""
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{hashed}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    if not password_hash or ':' not in password_hash:
        return False
    salt, hashed = password_hash.split(':', 1)
    return hashlib.sha256((salt + password).encode()).hexdigest() == hashed


def create_session(user_id: int) -> str:
    """Create a new session for a user."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(days=SESSION_EXPIRY_DAYS)
    
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sessions (token, user_id, expires_at)
            VALUES (?, ?, ?)
        """, (token, user_id, expires_at))
        
        # Update last_seen
        conn.execute("""
            UPDATE users SET last_seen = CURRENT_TIMESTAMP WHERE id = ?
        """, (user_id,))
    
    return token


def get_user_from_session(token: str) -> Optional[dict]:
    """Get user from session token."""
    if not token:
        return None
        
    with get_db() as conn:
        row = conn.execute("""
            SELECT u.id, u.username, u.display_name, u.role, u.created_at
            FROM sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.token = ? AND s.expires_at > CURRENT_TIMESTAMP
        """, (token,)).fetchone()
        
        if row:
            # Update last_seen
            conn.execute("""
                UPDATE users SET last_seen = CURRENT_TIMESTAMP WHERE id = ?
            """, (row["id"],))
            
            return {
                "id": row["id"],
                "username": row["username"],
                "display_name": row["display_name"],
                "role": row["role"],
                "created_at": row["created_at"]
            }
    
    return None


def delete_session(token: str):
    """Delete a session."""
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


async def get_current_user(request: Request) -> dict:
    """Dependency to get current user from session cookie."""
    if ANONYMOUS_MODE:
        return {
            "id": ANONYMOUS_USER_ID,
            "username": "anonymous",
            "display_name": "Anonymous User",
            "role": ROLE_ANNOTATOR,
            "is_anonymous": True
        }

    token = request.cookies.get("session")
    user = get_user_from_session(token)

    if not user:
        # If anonymous submissions allowed, return anonymous user
        if ALLOW_ANONYMOUS_SUBMISSIONS:
            return {
                "id": ANONYMOUS_USER_ID,
                "username": "anonymous",
                "display_name": "Anonymous User",
                "role": ROLE_ANNOTATOR,
                "is_anonymous": True
            }
        raise HTTPException(401, "Not authenticated. Please log in.")

    return user


async def get_optional_user(request: Request) -> Optional[dict]:
    """Dependency to get current user, or None if not authenticated."""
    if ANONYMOUS_MODE or ALLOW_ANONYMOUS_SUBMISSIONS:
        # Check for logged-in user first
        token = request.cookies.get("session")
        user = get_user_from_session(token)
        if user:
            return user
        # Fall back to anonymous
        return {
            "id": ANONYMOUS_USER_ID,
            "username": "anonymous",
            "display_name": "Anonymous User",
            "role": ROLE_ANNOTATOR,
            "is_anonymous": True
        }
    
    token = request.cookies.get("session")
    return get_user_from_session(token)


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Dependency to require admin role."""
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin access required.")
    return user


# ============================================================================
# Pydantic Models
# ============================================================================

class SpanSelection(BaseModel):
    """A single span selection within an example."""
    source: str = Field(..., description="Text source: 'premise' or 'hypothesis'", example="premise")
    word_index: int = Field(..., description="Token index within the source text", example=3)
    word_text: str = Field(..., description="The selected token text", example="running")
    char_start: Optional[int] = Field(None, description="Character start offset in original text")
    char_end: Optional[int] = Field(None, description="Character end offset in original text")


class LabelSubmission(BaseModel):
    """A label with its associated span selections."""
    label_name: str = Field(..., description="Name of the label", example="reasoning")
    label_color: str = Field(..., description="Hex color for the label", example="#3b82f6")
    spans: list[SpanSelection] = Field(..., description="List of selected spans for this label")


class AnnotationSubmission(BaseModel):
    """Complete annotation submission for an example."""
    example_id: str = Field(..., description="ID of the example being annotated", example="snli_12345")
    labels: list[LabelSubmission] = Field(..., description="List of labels with span selections")
    complexity_scores: Optional[dict] = Field(
        None,
        description="Complexity scores (1-100) for dimensions: reasoning, creativity, domain_knowledge, contextual, constraints, ambiguity",
        example={"reasoning": 75, "creativity": 40, "domain_knowledge": 50, "contextual": 60, "constraints": 30, "ambiguity": 45}
    )


class ExampleResponse(BaseModel):
    """Response containing an NLI example with tokenized text."""
    id: str = Field(..., description="Unique example identifier")
    dataset: str = Field(..., description="Source dataset name", example="snli")
    premise: str = Field(..., description="The premise text")
    hypothesis: str = Field(..., description="The hypothesis text")
    gold_label: Optional[int] = Field(None, description="Gold label as integer (0=entailment, 1=neutral, 2=contradiction)")
    gold_label_text: Optional[str] = Field(None, description="Gold label as text", example="entailment")
    premise_words: list[dict] = Field(..., description="Tokenized premise with character offsets")
    hypothesis_words: list[dict] = Field(..., description="Tokenized hypothesis with character offsets")
    existing_labels: list[dict] = Field(..., description="Previously saved labels for this example (by current user)")
    existing_scores: Optional[dict] = Field(None, description="Previously saved complexity scores (by current user)")
    lock_until: Optional[str] = Field(None, description="ISO timestamp when the lock on this example expires")
    pool_status: Optional[str] = Field(None, description="Question pool status: test, building, or zero_entry")
    auto_spans: Optional[dict] = Field(None, description="Auto-computed spans (Tier 0/1) for read-only display")


class UserCreate(BaseModel):
    """Request body for user registration."""
    username: str = Field(..., min_length=3, description="Username (min 3 characters)", example="annotator1")
    password: str = Field(..., min_length=6, description="Password (min 6 characters)")
    display_name: Optional[str] = Field(None, description="Display name (defaults to username)", example="Alice")


class UserLogin(BaseModel):
    """Request body for user login."""
    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")


class UserResponse(BaseModel):
    """Response containing user information."""
    id: int = Field(..., description="User ID")
    username: str = Field(..., description="Username")
    display_name: Optional[str] = Field(None, description="Display name")
    role: str = Field(ROLE_ANNOTATOR, description="User role (admin or annotator)")
    is_anonymous: bool = Field(False, description="True if running in anonymous mode")


class UserRoleUpdate(BaseModel):
    """Request body for updating a user's role."""
    role: str = Field(..., description="New role (admin or annotator)", example="admin")


class LabelSchemaResponse(BaseModel):
    """Response containing the label schema configuration."""
    categories: dict = Field(..., description="Label categories with their labels")
    system_labels: list[str] = Field(..., description="List of all system-defined label names")
    allow_custom: bool = Field(..., description="Whether custom labels are allowed")
    custom_tracking: bool = Field(..., description="Whether custom labels are tracked separately")


class LockStatusResponse(BaseModel):
    """Response containing lock status for an example."""
    example_id: str = Field(..., description="Example ID")
    locked: bool = Field(..., description="Whether the example is currently locked")
    locked_by: Optional[int] = Field(None, description="User ID of lock owner")
    locked_by_username: Optional[str] = Field(None, description="Username of lock owner")
    locked_until: Optional[str] = Field(None, description="ISO timestamp when lock expires")
    expires_in_seconds: Optional[int] = Field(None, description="Seconds until lock expires")
    is_own_lock: bool = Field(False, description="Whether current user owns the lock")


class ExampleAgreementResponse(BaseModel):
    """Response containing agreement metrics for an example."""
    example_id: str = Field(..., description="Example ID")
    annotation_count: int = Field(..., description="Number of annotators")
    agreement_score: Optional[float] = Field(None, description="Combined agreement score (0-1)")
    complexity_agreement: Optional[float] = Field(None, description="Complexity score agreement (0-1)")
    span_agreement: Optional[float] = Field(None, description="Span selection agreement (0-1)")
    pool_status: str = Field(..., description="Current pool status")
    consensus_threshold: int = Field(..., description="Annotations needed for consensus")
    needs_more_annotations: bool = Field(..., description="Whether more annotations are needed")


class AnnotatorAgreementResponse(BaseModel):
    """Response containing agreement metrics for an annotator."""
    user_id: int = Field(..., description="User ID")
    username: str = Field(..., description="Username")
    total_annotations: int = Field(..., description="Total annotations by this user")
    agreement_with_consensus: Optional[float] = Field(None, description="Agreement with consensus (0-1)")
    complexity_agreement: Optional[float] = Field(None, description="Complexity agreement (0-1)")
    span_agreement: Optional[float] = Field(None, description="Span agreement (0-1)")
    last_calculated: Optional[str] = Field(None, description="When metrics were last calculated")


class PoolStatsResponse(BaseModel):
    """Response containing question pool statistics."""
    test: int = Field(..., description="High-consensus calibration examples")
    building: int = Field(..., description="Examples being actively annotated")
    zero_entry: int = Field(..., description="Unannotated examples")
    total: int = Field(..., description="Total examples")
    consensus_threshold: int = Field(..., description="Annotations needed for consensus")
    agreement_threshold: float = Field(..., description="Agreement score for test pool")


class ReliabilityScoreResponse(BaseModel):
    """Response containing reliability score for an annotator."""
    user_id: int = Field(..., description="User ID")
    username: str = Field(..., description="Username")
    display_name: Optional[str] = Field(None, description="Display name")
    reliability_score: Optional[float] = Field(None, description="Reliability score (0-100)")
    calibration_status: str = Field(..., description="Calibration status: 'provisional' or 'calibrated'")
    scored_annotation_count: int = Field(..., description="Number of scored annotations")
    total_annotations: int = Field(..., description="Total annotations by this user")
    calibration_threshold: int = Field(..., description="Annotations needed for calibration")


class LeaderboardEntry(BaseModel):
    """A single entry in the leaderboard."""
    rank: int = Field(..., description="Rank position")
    user_id: int = Field(..., description="User ID")
    username: str = Field(..., description="Username")
    display_name: Optional[str] = Field(None, description="Display name")
    annotations: int = Field(..., description="Total annotations")
    reliability_score: Optional[float] = Field(None, description="Reliability score (0-100)")
    calibration_status: str = Field(..., description="Calibration status")
    is_current_user: bool = Field(False, description="Whether this is the requesting user")


class LeaderboardResponse(BaseModel):
    """Response containing the full leaderboard."""
    entries: list[LeaderboardEntry] = Field(..., description="Leaderboard entries")
    total_annotators: int = Field(..., description="Total number of annotators")
    calibration_threshold: int = Field(..., description="Annotations needed for calibration")


class ImportExample(BaseModel):
    """Single example for bulk import."""
    premise: str = Field(..., description="Premise text")
    hypothesis: str = Field(..., description="Hypothesis text")
    label: Optional[str] = Field(None, description="Gold label (entailment/neutral/contradiction)")
    id: Optional[str] = Field(None, description="Optional example ID (auto-generated if not provided)")
    source: Optional[str] = Field(None, description="Optional source/dataset metadata")


class ImportRequest(BaseModel):
    """Request body for bulk import."""
    examples: list[ImportExample] = Field(..., description="List of examples to import")
    dataset: str = Field("imported", description="Dataset name for imported examples")
    skip_duplicates: bool = Field(True, description="Skip examples with duplicate IDs (vs error)")


class ImportResponse(BaseModel):
    """Response from bulk import."""
    imported: int = Field(..., description="Number of examples successfully imported")
    skipped: int = Field(0, description="Number of duplicates skipped")
    errors: int = Field(0, description="Number of examples that failed to import")
    dataset: str = Field(..., description="Dataset name used")


# ============================================================================
# Data Loading
# ============================================================================

def load_examples_from_file(filepath: Path, limit: int = 1000) -> list[dict]:
    """Load examples from a JSONL file."""
    examples = []
    with open(filepath) as f:
        for i, line in enumerate(f):
            if i >= limit:
                break
            if line.strip():
                ex = json.loads(line)
                examples.append(ex)
    return examples


def ensure_examples_loaded(dataset: str, limit: int = 500):
    """Ensure examples from a dataset are in the database."""
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM examples WHERE dataset = ?",
            (dataset,)
        ).fetchone()[0]

        if count > 0:
            return count

        # Find and load the dataset file
        patterns = [f"{dataset}_*.jsonl", f"{dataset}.jsonl"]
        files = []
        for pattern in patterns:
            files.extend(DATA_DIR.glob(pattern))

        if not files:
            return 0

        loaded = 0
        for filepath in files:
            examples = load_examples_from_file(filepath, limit=limit)
            for ex in examples:
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO examples
                        (id, dataset, premise, hypothesis, gold_label, gold_label_text, source_file, pool_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'zero_entry')
                    """, (
                        ex.get("id", f"{dataset}_{loaded}"),
                        dataset,
                        ex.get("premise", ""),
                        ex.get("hypothesis", ""),
                        ex.get("label"),
                        ex.get("label_text"),
                        filepath.name
                    ))
                    loaded += 1
                except Exception as e:
                    print(f"Error loading example: {e}")

        return loaded


# ============================================================================
# API Endpoints - Root
# ============================================================================

@app.on_event("startup")
async def startup():
    """Initialize database and tokenizer on startup."""
    init_db()
    # Pre-load tokenizer (downloads on first run, then cached)
    get_tokenizer()
    # Pre-load some datasets
    for dataset in ["snli", "mnli", "anli"]:
        ensure_examples_loaded(dataset, limit=200)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    """Serve the main labeling interface."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# ============================================================================
# API Endpoints - Authentication
# ============================================================================

@app.post(
    "/api/auth/register",
    tags=["Authentication"],
    summary="Register a new user",
    description="Create a new user account. Returns a session cookie on success. Disabled when ANONYMOUS_MODE=1. If ADMIN_USER env var matches the username, user gets admin role.",
)
@limiter.limit(RATE_LIMIT_AUTH)
async def register(request: Request, user: UserCreate, response: Response):
    """Register a new user."""
    if ANONYMOUS_MODE:
        raise HTTPException(400, "Registration disabled in anonymous mode")
    
    if len(user.username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(user.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    
    with get_db() as conn:
        # Check if username exists
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (user.username,)
        ).fetchone()
        
        if existing:
            raise HTTPException(400, "Username already taken")
        
        # Determine role - admin if matches ADMIN_USER env var
        role = ROLE_ADMIN if user.username == ADMIN_USER else ROLE_ANNOTATOR
        
        # Create user
        password_hash = hash_password(user.password)
        cursor = conn.execute("""
            INSERT INTO users (username, password_hash, display_name, role)
            VALUES (?, ?, ?, ?)
        """, (user.username, password_hash, user.display_name or user.username, role))
        
        user_id = cursor.lastrowid
    
    # Create session
    token = create_session(user_id)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        max_age=SESSION_EXPIRY_DAYS * 24 * 60 * 60,
        samesite="lax"
    )
    
    return {
        "id": user_id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": role
    }


@app.post(
    "/api/auth/login",
    tags=["Authentication"],
    summary="Log in",
    description="Authenticate with username and password. Returns a session cookie on success.",
)
@limiter.limit(RATE_LIMIT_AUTH)
async def login(request: Request, user: UserLogin, response: Response):
    """Log in a user."""
    if ANONYMOUS_MODE:
        raise HTTPException(400, "Login disabled in anonymous mode")
    
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, password_hash, role FROM users WHERE username = ?",
            (user.username,)
        ).fetchone()
        
        if not row or not verify_password(user.password, row["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        
        user_id = row["id"]
        role = row["role"]
    
    # Create session
    token = create_session(user_id)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        max_age=SESSION_EXPIRY_DAYS * 24 * 60 * 60,
        samesite="lax"
    )
    
    return {"status": "logged_in", "user_id": user_id, "username": user.username, "role": role}


@app.post(
    "/api/auth/logout",
    tags=["Authentication"],
    summary="Log out",
    description="Invalidate the current session and clear the session cookie.",
)
async def logout(request: Request, response: Response):
    """Log out the current user."""
    token = request.cookies.get("session")
    if token:
        delete_session(token)
    response.delete_cookie("session")
    return {"status": "logged_out"}


@app.get(
    "/api/me",
    tags=["Authentication"],
    summary="Get current user",
    description="Get information about the currently authenticated user, including their role.",
    response_model=UserResponse,
)
async def get_me(user: dict = Depends(get_current_user)):
    """Get current user info."""
    return UserResponse(
        id=user["id"],
        username=user["username"],
        display_name=user.get("display_name"),
        role=user.get("role", ROLE_ANNOTATOR),
        is_anonymous=user.get("is_anonymous", False)
    )


@app.get(
    "/api/auth/status",
    tags=["Authentication"],
    summary="Get auth status",
    description="Check if the server is running in anonymous mode and whether registration is enabled.",
)
async def auth_status():
    """Get authentication status and mode."""
    return {
        "anonymous_mode": ANONYMOUS_MODE,
        "allow_anonymous_submissions": ALLOW_ANONYMOUS_SUBMISSIONS,
        "registration_enabled": not ANONYMOUS_MODE,
        "admin_bootstrap_configured": bool(ADMIN_USER)
    }


# ============================================================================
# API Endpoints - Auto Spans (Tiered Annotation)
# ============================================================================

@app.get(
    "/api/auto-spans/{example_id}",
    tags=["Auto Labels"],
    summary="Get auto-computed spans for an example",
    description="Retrieve Tier 0 (automatic) and Tier 1 (weak supervision) labels for an example.",
)
async def get_example_auto_spans(
    example_id: str,
    user: dict = Depends(get_current_user)
):
    """Get auto-computed spans for an example."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT premise, hypothesis FROM examples WHERE id = ?",
            (example_id,)
        ).fetchone()

        if not row:
            raise HTTPException(404, f"Example not found: {example_id}")

        auto_spans = ensure_auto_spans_computed(conn, example_id, row["premise"], row["hypothesis"])
        return {"example_id": example_id, "auto_spans": auto_spans}


@app.post(
    "/api/admin/compute-auto-spans",
    tags=["Admin"],
    summary="Batch compute auto-spans (admin)",
    description="Compute Tier 0 auto-spans for all examples or a specific dataset. Admin only.",
)
async def admin_compute_auto_spans(
    dataset: str = Query(None, description="Filter by dataset (compute all if not specified)"),
    limit: int = Query(1000, description="Maximum examples to process"),
    admin: dict = Depends(require_admin)
):
    """Batch compute Tier 0 auto-spans for examples."""
    with get_db() as conn:
        # Find examples without auto-spans
        query = """
            SELECT e.id, e.premise, e.hypothesis
            FROM examples e
            WHERE e.id NOT IN (SELECT DISTINCT example_id FROM auto_spans)
        """
        params = []

        if dataset:
            query += " AND e.dataset = ?"
            params.append(dataset)

        query += f" LIMIT {limit}"

        rows = conn.execute(query, params).fetchall()
        computed = 0

        for row in rows:
            premise_tokens = tokenize_text(row["premise"])
            hypothesis_tokens = tokenize_text(row["hypothesis"])
            auto_labels = compute_tier0_labels(row["premise"], row["hypothesis"], premise_tokens, hypothesis_tokens)
            store_auto_spans(conn, row["id"], auto_labels, tier=0)
            computed += 1

        conn.commit()

        return {
            "computed": computed,
            "dataset": dataset,
            "message": f"Computed Tier 0 auto-spans for {computed} examples"
        }


# ============================================================================
# API Endpoints - Admin
# ============================================================================

@app.get(
    "/api/admin/users",
    tags=["Admin"],
    summary="List all users (admin)",
    description="List all users with detailed information including roles. Admin only.",
)
async def admin_list_users(admin: dict = Depends(require_admin)):
    """List all users with admin details."""
    with get_db() as conn:
        users = conn.execute("""
            SELECT u.id, u.username, u.display_name, u.role, u.created_at, u.last_seen,
                   COUNT(DISTINCT c.example_id) as annotations
            FROM users u
            LEFT JOIN complexity_scores c ON u.id = c.annotator_id
            GROUP BY u.id
            ORDER BY u.id
        """).fetchall()
        
        return {
            "users": [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "display_name": row["display_name"],
                    "role": row["role"],
                    "created_at": row["created_at"],
                    "last_seen": row["last_seen"],
                    "annotations": row["annotations"],
                    "is_system_user": row["id"] in (ANONYMOUS_USER_ID, LEGACY_USER_ID)
                }
                for row in users
            ],
            "total": len(users)
        }


@app.get(
    "/api/admin/user/{user_id}",
    tags=["Admin"],
    summary="Get user details (admin)",
    description="Get detailed information about a specific user. Admin only.",
)
async def admin_get_user(user_id: int, admin: dict = Depends(require_admin)):
    """Get detailed user information."""
    with get_db() as conn:
        user = conn.execute("""
            SELECT id, username, display_name, role, created_at, last_seen
            FROM users WHERE id = ?
        """, (user_id,)).fetchone()
        
        if not user:
            raise HTTPException(404, f"User not found: {user_id}")
        
        # Get annotation stats
        stats = conn.execute("""
            SELECT 
                COUNT(DISTINCT c.example_id) as annotations,
                COUNT(DISTINCT s.example_id) as skipped,
                COUNT(DISTINCT CASE WHEN l.is_custom = 1 THEN l.id END) as custom_labels
            FROM users u
            LEFT JOIN complexity_scores c ON u.id = c.annotator_id
            LEFT JOIN skipped s ON u.id = s.annotator_id
            LEFT JOIN labels l ON u.id = l.annotator_id
            WHERE u.id = ?
        """, (user_id,)).fetchone()
        
        # Get agreement metrics
        agreement = conn.execute("""
            SELECT agreement_with_consensus, complexity_agreement, span_agreement
            FROM annotator_agreement WHERE user_id = ?
        """, (user_id,)).fetchone()
        
        return {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "created_at": user["created_at"],
            "last_seen": user["last_seen"],
            "is_system_user": user["id"] in (ANONYMOUS_USER_ID, LEGACY_USER_ID),
            "stats": {
                "annotations": stats["annotations"],
                "skipped": stats["skipped"],
                "custom_labels": stats["custom_labels"]
            },
            "agreement": {
                "agreement_with_consensus": agreement["agreement_with_consensus"] if agreement else None,
                "complexity_agreement": agreement["complexity_agreement"] if agreement else None,
                "span_agreement": agreement["span_agreement"] if agreement else None,
            } if agreement else None
        }


@app.put(
    "/api/admin/user/{user_id}/role",
    tags=["Admin"],
    summary="Update user role (admin)",
    description="Change a user's role. Cannot modify system users. Admin only.",
)
async def admin_update_user_role(
    user_id: int,
    role_update: UserRoleUpdate,
    admin: dict = Depends(require_admin)
):
    """Update a user's role."""
    # Validate role
    if role_update.role not in (ROLE_ADMIN, ROLE_ANNOTATOR):
        raise HTTPException(400, f"Invalid role: {role_update.role}. Must be '{ROLE_ADMIN}' or '{ROLE_ANNOTATOR}'.")
    
    # Prevent modifying system users
    if user_id in (ANONYMOUS_USER_ID, LEGACY_USER_ID):
        raise HTTPException(400, "Cannot modify system user roles.")
    
    # Prevent admin from demoting themselves
    if user_id == admin["id"] and role_update.role != ROLE_ADMIN:
        raise HTTPException(400, "Cannot demote yourself. Ask another admin.")
    
    with get_db() as conn:
        user = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        
        if not user:
            raise HTTPException(404, f"User not found: {user_id}")
        
        old_role = user["role"]
        
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role_update.role, user_id))
        
        return {
            "status": "updated",
            "user_id": user_id,
            "username": user["username"],
            "old_role": old_role,
            "new_role": role_update.role
        }


@app.get(
    "/api/admin/calibration",
    tags=["Admin"],
    summary="Get calibration routing config (admin)",
    description="View current calibration routing configuration and test pool statistics.",
)
async def admin_get_calibration_config(admin: dict = Depends(require_admin)):
    """Get calibration routing configuration and statistics."""
    with get_db() as conn:
        # Get test pool statistics
        test_count = conn.execute(
            "SELECT COUNT(*) as count FROM examples WHERE pool_status = 'test'"
        ).fetchone()["count"]

        # Get counts by calibration status
        calibration_stats = conn.execute("""
            SELECT calibration_status, COUNT(*) as count
            FROM annotator_agreement
            GROUP BY calibration_status
        """).fetchall()

        status_counts = {row["calibration_status"]: row["count"] for row in calibration_stats}

        return {
            "enabled": CALIBRATION_ROUTING_ENABLED,
            "test_ratio_new": CALIBRATION_TEST_RATIO_NEW,
            "test_ratio_established": CALIBRATION_TEST_RATIO_ESTABLISHED,
            "calibration_threshold": CALIBRATION_MIN_SCORED,
            "test_pool_size": test_count,
            "annotator_counts": {
                "provisional": status_counts.get("provisional", 0),
                "calibrated": status_counts.get("calibrated", 0)
            }
        }


# ============================================================================
# Gold Example Management Endpoints
# ============================================================================

class GoldAnnotationCreate(BaseModel):
    """Request model for creating a gold annotation."""
    example_id: str
    gold_complexity_scores: dict = Field(..., description="Gold standard complexity scores")
    gold_labels: Optional[list] = Field(None, description="Gold standard labels (optional)")
    explanation: Optional[str] = Field(None, description="Explanation of why this is the correct answer")


@app.get(
    "/api/admin/gold",
    tags=["Admin"],
    summary="List gold examples",
    description="List all examples marked as gold standard for training.",
)
async def list_gold_examples(
    admin: dict = Depends(require_admin)
):
    """List all gold examples."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                g.example_id,
                g.gold_complexity_scores,
                g.gold_labels,
                g.explanation,
                g.created_by,
                g.created_at,
                e.dataset,
                e.premise,
                e.hypothesis,
                u.username as created_by_username
            FROM gold_annotations g
            JOIN examples e ON g.example_id = e.id
            JOIN users u ON g.created_by = u.id
            ORDER BY g.created_at DESC
        """).fetchall()

        gold_examples = []
        for row in rows:
            gold_examples.append({
                "example_id": row["example_id"],
                "gold_complexity_scores": json.loads(row["gold_complexity_scores"]),
                "gold_labels": json.loads(row["gold_labels"]) if row["gold_labels"] else None,
                "explanation": row["explanation"],
                "dataset": row["dataset"],
                "premise": row["premise"],
                "hypothesis": row["hypothesis"],
                "created_by": row["created_by_username"],
                "created_at": row["created_at"]
            })

        return {
            "gold_examples": gold_examples,
            "total": len(gold_examples),
            "training_min_examples": TRAINING_MIN_EXAMPLES
        }


@app.post(
    "/api/admin/gold",
    tags=["Admin"],
    summary="Mark example as gold",
    description="Mark an example as a gold standard with correct answers for training.",
)
async def create_gold_example(
    gold: GoldAnnotationCreate,
    admin: dict = Depends(require_admin)
):
    """Create a gold annotation for an example."""
    with get_db() as conn:
        # Verify example exists
        example = conn.execute(
            "SELECT id FROM examples WHERE id = ?", (gold.example_id,)
        ).fetchone()
        if not example:
            raise HTTPException(status_code=404, detail="Example not found")

        # Check if already gold
        existing = conn.execute(
            "SELECT example_id FROM gold_annotations WHERE example_id = ?",
            (gold.example_id,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Example is already marked as gold")

        # Insert gold annotation
        conn.execute("""
            INSERT INTO gold_annotations (example_id, gold_complexity_scores, gold_labels, explanation, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (
            gold.example_id,
            json.dumps(gold.gold_complexity_scores),
            json.dumps(gold.gold_labels) if gold.gold_labels else None,
            gold.explanation,
            admin["id"]
        ))

        # Move example to test pool (gold examples are used for calibration)
        conn.execute(
            "UPDATE examples SET pool_status = 'test' WHERE id = ?",
            (gold.example_id,)
        )

        conn.commit()

        return {
            "success": True,
            "example_id": gold.example_id,
            "message": "Example marked as gold and moved to test pool"
        }


@app.put(
    "/api/admin/gold/{example_id}",
    tags=["Admin"],
    summary="Update gold example",
    description="Update the gold annotation for an example.",
)
async def update_gold_example(
    example_id: str,
    gold: GoldAnnotationCreate,
    admin: dict = Depends(require_admin)
):
    """Update a gold annotation."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT example_id FROM gold_annotations WHERE example_id = ?",
            (example_id,)
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Gold annotation not found")

        conn.execute("""
            UPDATE gold_annotations
            SET gold_complexity_scores = ?, gold_labels = ?, explanation = ?
            WHERE example_id = ?
        """, (
            json.dumps(gold.gold_complexity_scores),
            json.dumps(gold.gold_labels) if gold.gold_labels else None,
            gold.explanation,
            example_id
        ))
        conn.commit()

        return {
            "success": True,
            "example_id": example_id,
            "message": "Gold annotation updated"
        }


@app.delete(
    "/api/admin/gold/{example_id}",
    tags=["Admin"],
    summary="Remove gold status",
    description="Remove gold status from an example.",
)
async def delete_gold_example(
    example_id: str,
    admin: dict = Depends(require_admin)
):
    """Remove gold status from an example."""
    with get_db() as conn:
        result = conn.execute(
            "DELETE FROM gold_annotations WHERE example_id = ?",
            (example_id,)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Gold annotation not found")

        # Move back to building pool if it has annotations, otherwise zero_entry
        has_annotations = conn.execute(
            "SELECT COUNT(*) as count FROM complexity_scores WHERE example_id = ?",
            (example_id,)
        ).fetchone()["count"]

        new_pool = "building" if has_annotations > 0 else "zero_entry"
        conn.execute(
            "UPDATE examples SET pool_status = ? WHERE id = ?",
            (new_pool, example_id)
        )

        conn.commit()

        return {
            "success": True,
            "example_id": example_id,
            "new_pool_status": new_pool,
            "message": "Gold status removed"
        }


# ============================================================================
# Training Mode Endpoints
# ============================================================================

class TrainingSubmission(BaseModel):
    """Request model for submitting a training annotation."""
    example_id: str
    complexity_scores: dict = Field(..., description="Submitted complexity scores")
    labels: Optional[list] = Field(None, description="Submitted labels (optional)")


@app.get(
    "/api/training/status",
    tags=["Training"],
    summary="Get training status",
    description="Check if the current user needs training and their progress.",
)
async def get_training_status(user: dict = Depends(get_current_user)):
    """Get current user's training status."""
    if not TRAINING_ENABLED:
        return {
            "training_enabled": False,
            "training_required": False,
            "completed": 0,
            "required": 0
        }

    with get_db() as conn:
        # Check if user needs training
        user_row = conn.execute(
            "SELECT training_required, training_completed_at FROM users WHERE id = ?",
            (user["id"],)
        ).fetchone()

        training_required = bool(user_row["training_required"]) if user_row else True

        # Get training progress
        completed = conn.execute(
            "SELECT COUNT(*) as count FROM training_submissions WHERE user_id = ?",
            (user["id"],)
        ).fetchone()["count"]

        # Get total gold examples available
        total_gold = conn.execute(
            "SELECT COUNT(*) as count FROM gold_annotations"
        ).fetchone()["count"]

        return {
            "training_enabled": TRAINING_ENABLED,
            "training_required": training_required,
            "completed": completed,
            "required": TRAINING_MIN_EXAMPLES,
            "available_gold_examples": total_gold,
            "training_completed_at": user_row["training_completed_at"] if user_row else None
        }


@app.get(
    "/api/training/next",
    tags=["Training"],
    summary="Get next training example",
    description="Get the next gold example for training. Only returns examples the user hasn't completed.",
)
async def get_next_training_example(user: dict = Depends(get_current_user)):
    """Get next training example for the user."""
    if not TRAINING_ENABLED:
        raise HTTPException(status_code=400, detail="Training mode is disabled")

    with get_db() as conn:
        # Find gold examples the user hasn't completed
        row = conn.execute("""
            SELECT
                g.example_id,
                e.dataset,
                e.premise,
                e.hypothesis,
                e.gold_label_text
            FROM gold_annotations g
            JOIN examples e ON g.example_id = e.id
            WHERE g.example_id NOT IN (
                SELECT example_id FROM training_submissions WHERE user_id = ?
            )
            ORDER BY RANDOM()
            LIMIT 1
        """, (user["id"],)).fetchone()

        if not row:
            # Check if training is complete
            completed = conn.execute(
                "SELECT COUNT(*) as count FROM training_submissions WHERE user_id = ?",
                (user["id"],)
            ).fetchone()["count"]

            if completed >= TRAINING_MIN_EXAMPLES:
                return {
                    "training_complete": True,
                    "completed": completed,
                    "required": TRAINING_MIN_EXAMPLES
                }
            else:
                raise HTTPException(
                    status_code=404,
                    detail="No more training examples available. Contact admin to add more gold examples."
                )

        # Tokenize the example
        tokenizer = get_tokenizer()
        tokens = tokenize_text(tokenizer, row["premise"], row["hypothesis"])

        return {
            "training_complete": False,
            "example": {
                "id": row["example_id"],
                "dataset": row["dataset"],
                "premise": row["premise"],
                "hypothesis": row["hypothesis"],
                "gold_label": row["gold_label_text"],
                "tokens": tokens
            }
        }


@app.post(
    "/api/training/submit",
    tags=["Training"],
    summary="Submit training annotation",
    description="Submit an annotation for a training example. Returns feedback comparing to gold standard.",
)
async def submit_training_annotation(
    submission: TrainingSubmission,
    user: dict = Depends(get_current_user)
):
    """Submit a training annotation and receive feedback."""
    if not TRAINING_ENABLED:
        raise HTTPException(status_code=400, detail="Training mode is disabled")

    with get_db() as conn:
        # Get gold annotation
        gold = conn.execute("""
            SELECT gold_complexity_scores, gold_labels, explanation
            FROM gold_annotations WHERE example_id = ?
        """, (submission.example_id,)).fetchone()

        if not gold:
            raise HTTPException(status_code=404, detail="Not a gold example")

        # Check if already submitted
        existing = conn.execute(
            "SELECT id FROM training_submissions WHERE user_id = ? AND example_id = ?",
            (user["id"], submission.example_id)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Already submitted for this example")

        # Parse gold scores
        gold_scores = json.loads(gold["gold_complexity_scores"])
        gold_labels = json.loads(gold["gold_labels"]) if gold["gold_labels"] else []

        # Calculate accuracy (simple average of score differences)
        score_diffs = []
        for key in ["reasoning", "creativity", "domain_knowledge", "contextual", "constraints", "ambiguity"]:
            if key in gold_scores and key in submission.complexity_scores:
                diff = abs(gold_scores[key] - submission.complexity_scores[key])
                # Normalize to 0-1 where 0 is perfect match
                score_diffs.append(1 - (diff / 100))

        accuracy = sum(score_diffs) / len(score_diffs) if score_diffs else 0

        # Save submission
        conn.execute("""
            INSERT INTO training_submissions (user_id, example_id, submitted_complexity_scores, submitted_labels, accuracy_score)
            VALUES (?, ?, ?, ?, ?)
        """, (
            user["id"],
            submission.example_id,
            json.dumps(submission.complexity_scores),
            json.dumps(submission.labels) if submission.labels else None,
            accuracy
        ))

        # Check if training is now complete
        completed = conn.execute(
            "SELECT COUNT(*) as count FROM training_submissions WHERE user_id = ?",
            (user["id"],)
        ).fetchone()["count"]

        training_complete = completed >= TRAINING_MIN_EXAMPLES

        if training_complete:
            # Calculate overall training accuracy
            avg_accuracy = conn.execute(
                "SELECT AVG(accuracy_score) as avg FROM training_submissions WHERE user_id = ?",
                (user["id"],)
            ).fetchone()["avg"]

            # Mark training as complete
            conn.execute("""
                UPDATE users
                SET training_required = 0, training_completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (user["id"],))

            # Set initial reliability score based on training accuracy
            conn.execute("""
                INSERT INTO annotator_agreement (user_id, reliability_score, calibration_status)
                VALUES (?, ?, 'provisional')
                ON CONFLICT(user_id) DO UPDATE SET
                    reliability_score = excluded.reliability_score
            """, (user["id"], avg_accuracy * 100))

        conn.commit()

        # Build feedback response
        return {
            "success": True,
            "accuracy": round(accuracy * 100, 1),
            "feedback": {
                "your_scores": submission.complexity_scores,
                "gold_scores": gold_scores,
                "your_labels": submission.labels,
                "gold_labels": gold_labels,
                "explanation": gold["explanation"]
            },
            "progress": {
                "completed": completed,
                "required": TRAINING_MIN_EXAMPLES,
                "training_complete": training_complete
            }
        }


@app.get(
    "/api/training/progress",
    tags=["Training"],
    summary="Get training progress",
    description="Get detailed training progress including accuracy per example.",
)
async def get_training_progress(user: dict = Depends(get_current_user)):
    """Get detailed training progress."""
    with get_db() as conn:
        submissions = conn.execute("""
            SELECT
                ts.example_id,
                ts.accuracy_score,
                ts.created_at,
                e.dataset
            FROM training_submissions ts
            JOIN examples e ON ts.example_id = e.id
            WHERE ts.user_id = ?
            ORDER BY ts.created_at
        """, (user["id"],)).fetchall()

        total_gold = conn.execute(
            "SELECT COUNT(*) as count FROM gold_annotations"
        ).fetchone()["count"]

        avg_accuracy = None
        if submissions:
            avg_accuracy = sum(s["accuracy_score"] for s in submissions) / len(submissions)

        return {
            "submissions": [
                {
                    "example_id": s["example_id"],
                    "dataset": s["dataset"],
                    "accuracy": round(s["accuracy_score"] * 100, 1),
                    "submitted_at": s["created_at"]
                }
                for s in submissions
            ],
            "completed": len(submissions),
            "required": TRAINING_MIN_EXAMPLES,
            "available": total_gold,
            "average_accuracy": round(avg_accuracy * 100, 1) if avg_accuracy else None,
            "training_complete": len(submissions) >= TRAINING_MIN_EXAMPLES
        }


@app.get(
    "/api/admin/dashboard",
    tags=["Admin"],
    summary="Get admin dashboard metrics",
    description="""
Comprehensive dashboard metrics for project administrators.

**Metrics included:**
- Total annotations across all users
- Annotations per day/week (activity data)
- Per-dataset completion percentages
- Per-user contribution breakdown with reliability scores
- Label distribution
- Average complexity scores
- Active user counts (24h/7d)
- Quality indicators (overlap, disagreement hotspots)
- Question pool status

Filterable by date range and dataset.
    """,
)
async def admin_dashboard(
    dataset: Optional[str] = Query(None, description="Filter by dataset"),
    days: int = Query(30, description="Number of days for activity data", ge=1, le=365),
    admin: dict = Depends(require_admin)
):
    """Get comprehensive admin dashboard metrics."""

    with get_db() as conn:
        # Calculate date boundaries
        now = datetime.now()
        start_date = (now - timedelta(days=days)).isoformat()
        day_ago = (now - timedelta(days=1)).isoformat()
        week_ago = (now - timedelta(days=7)).isoformat()

        # Base dataset filter
        dataset_filter = "AND e.dataset = ?" if dataset else ""
        dataset_params = [dataset] if dataset else []

        # =====================================================================
        # Overview Metrics
        # =====================================================================

        # Total annotations
        total_annotations = conn.execute(f"""
            SELECT COUNT(*) as count FROM complexity_scores c
            JOIN examples e ON c.example_id = e.id
            WHERE 1=1 {dataset_filter}
        """, dataset_params).fetchone()["count"]

        # Total examples
        total_examples = conn.execute(f"""
            SELECT COUNT(*) as count FROM examples e
            WHERE 1=1 {dataset_filter}
        """, dataset_params).fetchone()["count"]

        # Annotated examples (unique)
        annotated_examples = conn.execute(f"""
            SELECT COUNT(DISTINCT c.example_id) as count
            FROM complexity_scores c
            JOIN examples e ON c.example_id = e.id
            WHERE 1=1 {dataset_filter}
        """, dataset_params).fetchone()["count"]

        # Completion percentage
        completion_pct = round((annotated_examples / total_examples * 100), 1) if total_examples > 0 else 0

        # =====================================================================
        # Activity Over Time
        # =====================================================================

        # Annotations per day (last N days)
        activity_query = f"""
            SELECT DATE(c.created_at) as date, COUNT(*) as count
            FROM complexity_scores c
            JOIN examples e ON c.example_id = e.id
            WHERE c.created_at >= ? {dataset_filter}
            GROUP BY DATE(c.created_at)
            ORDER BY date
        """
        activity_rows = conn.execute(activity_query, [start_date] + dataset_params).fetchall()
        daily_activity = [{"date": row["date"], "count": row["count"]} for row in activity_rows]

        # Weekly aggregation
        weekly_query = f"""
            SELECT strftime('%Y-W%W', c.created_at) as week, COUNT(*) as count
            FROM complexity_scores c
            JOIN examples e ON c.example_id = e.id
            WHERE c.created_at >= ? {dataset_filter}
            GROUP BY week
            ORDER BY week
        """
        weekly_rows = conn.execute(weekly_query, [start_date] + dataset_params).fetchall()
        weekly_activity = [{"week": row["week"], "count": row["count"]} for row in weekly_rows]

        # =====================================================================
        # Per-Dataset Breakdown
        # =====================================================================

        dataset_stats = conn.execute("""
            SELECT
                e.dataset,
                COUNT(DISTINCT e.id) as total_examples,
                COUNT(DISTINCT c.example_id) as annotated_examples,
                COUNT(c.id) as total_annotations
            FROM examples e
            LEFT JOIN complexity_scores c ON e.id = c.example_id
            GROUP BY e.dataset
            ORDER BY e.dataset
        """).fetchall()

        datasets = []
        for row in dataset_stats:
            annotated = row["annotated_examples"] or 0
            total = row["total_examples"]
            datasets.append({
                "name": row["dataset"],
                "total_examples": total,
                "annotated_examples": annotated,
                "total_annotations": row["total_annotations"] or 0,
                "completion_pct": round((annotated / total * 100), 1) if total > 0 else 0
            })

        # =====================================================================
        # Per-User Breakdown
        # =====================================================================

        user_stats = conn.execute(f"""
            SELECT
                u.id, u.username, u.display_name, u.role, u.last_seen,
                COUNT(DISTINCT c.example_id) as annotations,
                aa.reliability_score,
                aa.calibration_status,
                aa.agreement_with_consensus
            FROM users u
            LEFT JOIN complexity_scores c ON u.id = c.annotator_id
            LEFT JOIN annotator_agreement aa ON u.id = aa.user_id
            LEFT JOIN examples e ON c.example_id = e.id
            WHERE u.id NOT IN (?, ?) {dataset_filter}
            GROUP BY u.id
            HAVING annotations > 0
            ORDER BY annotations DESC
        """, [ANONYMOUS_USER_ID, LEGACY_USER_ID] + dataset_params).fetchall()

        annotators = []
        for row in user_stats:
            annotators.append({
                "id": row["id"],
                "username": row["username"],
                "display_name": row["display_name"],
                "role": row["role"],
                "annotations": row["annotations"],
                "reliability_score": row["reliability_score"],
                "calibration_status": row["calibration_status"],
                "agreement_with_consensus": row["agreement_with_consensus"],
                "last_seen": row["last_seen"]
            })

        # =====================================================================
        # Active Users
        # =====================================================================

        active_24h = conn.execute(f"""
            SELECT COUNT(DISTINCT c.annotator_id) as count
            FROM complexity_scores c
            JOIN examples e ON c.example_id = e.id
            WHERE c.created_at >= ? {dataset_filter}
        """, [day_ago] + dataset_params).fetchone()["count"]

        active_7d = conn.execute(f"""
            SELECT COUNT(DISTINCT c.annotator_id) as count
            FROM complexity_scores c
            JOIN examples e ON c.example_id = e.id
            WHERE c.created_at >= ? {dataset_filter}
        """, [week_ago] + dataset_params).fetchone()["count"]

        # =====================================================================
        # Label Distribution
        # =====================================================================

        label_dist = conn.execute(f"""
            SELECT l.label_name, l.is_custom, COUNT(*) as count
            FROM labels l
            JOIN examples e ON l.example_id = e.id
            WHERE 1=1 {dataset_filter}
            GROUP BY l.label_name, l.is_custom
            ORDER BY count DESC
        """, dataset_params).fetchall()

        labels = [
            {"name": row["label_name"], "is_custom": bool(row["is_custom"]), "count": row["count"]}
            for row in label_dist
        ]

        # =====================================================================
        # Complexity Score Averages
        # =====================================================================

        complexity_avgs = conn.execute(f"""
            SELECT
                AVG(c.reasoning) as reasoning,
                AVG(c.creativity) as creativity,
                AVG(c.domain_knowledge) as domain_knowledge,
                AVG(c.contextual) as contextual,
                AVG(c.constraints) as constraints,
                AVG(c.ambiguity) as ambiguity
            FROM complexity_scores c
            JOIN examples e ON c.example_id = e.id
            WHERE 1=1 {dataset_filter}
        """, dataset_params).fetchone()

        complexity_scores = {
            "reasoning": round(complexity_avgs["reasoning"], 1) if complexity_avgs["reasoning"] else None,
            "creativity": round(complexity_avgs["creativity"], 1) if complexity_avgs["creativity"] else None,
            "domain_knowledge": round(complexity_avgs["domain_knowledge"], 1) if complexity_avgs["domain_knowledge"] else None,
            "contextual": round(complexity_avgs["contextual"], 1) if complexity_avgs["contextual"] else None,
            "constraints": round(complexity_avgs["constraints"], 1) if complexity_avgs["constraints"] else None,
            "ambiguity": round(complexity_avgs["ambiguity"], 1) if complexity_avgs["ambiguity"] else None,
        }

        # =====================================================================
        # Quality Indicators
        # =====================================================================

        # Examples with multiple annotations (overlap)
        overlap_stats = conn.execute(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN annotation_count >= 2 THEN 1 ELSE 0 END) as with_overlap,
                SUM(CASE WHEN annotation_count >= {CONSENSUS_THRESHOLD} THEN 1 ELSE 0 END) as at_consensus
            FROM example_agreement ea
            JOIN examples e ON ea.example_id = e.id
            WHERE 1=1 {dataset_filter}
        """, dataset_params).fetchone()

        # Disagreement hotspots (low agreement with multiple annotations)
        disagreement_hotspots = conn.execute(f"""
            SELECT ea.example_id, e.dataset, ea.annotation_count, ea.agreement_score
            FROM example_agreement ea
            JOIN examples e ON ea.example_id = e.id
            WHERE ea.annotation_count >= 3 AND ea.agreement_score < 0.5 {dataset_filter}
            ORDER BY ea.agreement_score ASC
            LIMIT 10
        """, dataset_params).fetchall()

        hotspots = [
            {
                "example_id": row["example_id"],
                "dataset": row["dataset"],
                "annotation_count": row["annotation_count"],
                "agreement_score": row["agreement_score"]
            }
            for row in disagreement_hotspots
        ]

        # Custom label usage
        custom_label_count = conn.execute(f"""
            SELECT COUNT(*) as count FROM labels l
            JOIN examples e ON l.example_id = e.id
            WHERE l.is_custom = 1 {dataset_filter}
        """, dataset_params).fetchone()["count"]

        # =====================================================================
        # Question Pool Status
        # =====================================================================

        pool_stats = conn.execute(f"""
            SELECT pool_status, COUNT(*) as count
            FROM examples e
            WHERE 1=1 {dataset_filter}
            GROUP BY pool_status
        """, dataset_params).fetchall()

        pools = {row["pool_status"]: row["count"] for row in pool_stats}

        # =====================================================================
        # Build Response
        # =====================================================================

        return {
            "generated_at": now.isoformat(),
            "filters": {
                "dataset": dataset,
                "days": days
            },
            "overview": {
                "total_annotations": total_annotations,
                "total_examples": total_examples,
                "annotated_examples": annotated_examples,
                "completion_pct": completion_pct,
                "active_users_24h": active_24h,
                "active_users_7d": active_7d,
                "total_annotators": len(annotators)
            },
            "activity": {
                "daily": daily_activity,
                "weekly": weekly_activity
            },
            "datasets": datasets,
            "annotators": annotators,
            "labels": labels,
            "complexity_averages": complexity_scores,
            "quality": {
                "examples_with_overlap": overlap_stats["with_overlap"] or 0,
                "examples_at_consensus": overlap_stats["at_consensus"] or 0,
                "disagreement_hotspots": hotspots,
                "custom_label_usage": custom_label_count
            },
            "pools": {
                "test": pools.get("test", 0),
                "building": pools.get("building", 0),
                "zero_entry": pools.get("zero_entry", 0),
                "consensus_threshold": CONSENSUS_THRESHOLD
            }
        }


@app.get(
    "/api/admin/user/{user_id}/annotations",
    tags=["Admin"],
    summary="Get user's annotations for review (admin)",
    description="""Get a user's annotations with consensus comparison for quality review.

Supports filtering by:
- `dataset`: Filter by specific dataset
- `disagreements_only`: Only show examples where user disagreed with consensus
- `limit/offset`: Pagination
""",
)
async def admin_get_user_annotations(
    user_id: int,
    dataset: Optional[str] = Query(None, description="Filter by dataset"),
    disagreements_only: bool = Query(False, description="Only show disagreements"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    admin: dict = Depends(require_admin)
):
    """Get user's annotations with consensus comparison."""
    with get_db() as conn:
        # Verify user exists
        user = conn.execute(
            "SELECT id, username, display_name FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if not user:
            raise HTTPException(404, f"User not found: {user_id}")

        # Build query for user's annotations
        dataset_filter = "AND e.dataset = ?" if dataset else ""
        dataset_params = [dataset] if dataset else []

        # Get user's annotations with example data
        query = f"""
            SELECT
                e.id as example_id,
                e.premise,
                e.hypothesis,
                e.dataset,
                cs.score as user_complexity,
                GROUP_CONCAT(DISTINCT sl.label) as user_labels,
                cs.created_at
            FROM complexity_scores cs
            JOIN examples e ON cs.example_id = e.id
            LEFT JOIN span_labels sl ON sl.example_id = e.id AND sl.annotator_id = cs.annotator_id
            WHERE cs.annotator_id = ?
            {dataset_filter}
            GROUP BY e.id, cs.id
            ORDER BY cs.created_at DESC
        """

        all_annotations = conn.execute(
            query, [user_id] + dataset_params
        ).fetchall()

        # For each annotation, get consensus data
        results = []
        for ann in all_annotations:
            example_id = ann["example_id"]

            # Get consensus labels for this example
            consensus_labels = conn.execute("""
                SELECT label, COUNT(*) as count
                FROM span_labels
                WHERE example_id = ?
                GROUP BY label
                HAVING count >= ?
            """, (example_id, CONSENSUS_THRESHOLD // 2 + 1)).fetchall()

            consensus_label_set = set(row["label"] for row in consensus_labels) if consensus_labels else set()
            user_label_set = set(ann["user_labels"].split(",")) if ann["user_labels"] else set()

            # Calculate disagreement
            missing_labels = consensus_label_set - user_label_set
            extra_labels = user_label_set - consensus_label_set
            disagreement_severity = len(missing_labels) + len(extra_labels)

            # Filter if disagreements_only
            if disagreements_only and disagreement_severity == 0:
                continue

            # Get consensus complexity
            consensus_complexity = conn.execute("""
                SELECT AVG(score) as avg_complexity
                FROM complexity_scores
                WHERE example_id = ?
            """, (example_id,)).fetchone()

            results.append({
                "example_id": example_id,
                "premise": ann["premise"],
                "hypothesis": ann["hypothesis"],
                "dataset": ann["dataset"],
                "user_labels": list(user_label_set) if user_label_set else [],
                "consensus_labels": list(consensus_label_set) if consensus_label_set else [],
                "user_complexity": ann["user_complexity"],
                "consensus_complexity": consensus_complexity["avg_complexity"] if consensus_complexity else None,
                "disagreement_severity": disagreement_severity,
                "missing_labels": list(missing_labels),
                "extra_labels": list(extra_labels),
                "created_at": ann["created_at"]
            })

        # Apply pagination
        total = len(results)
        paginated = results[offset:offset + limit]

        return {
            "user": {
                "id": user["id"],
                "username": user["username"],
                "display_name": user["display_name"]
            },
            "annotations": paginated,
            "total": total,
            "limit": limit,
            "offset": offset
        }


@app.get(
    "/api/admin/user/{user_id}/disagreements",
    tags=["Admin"],
    summary="Get user's disagreements with consensus (admin)",
    description="Get examples where user's annotation significantly differs from consensus.",
)
async def admin_get_user_disagreements(
    user_id: int,
    dataset: Optional[str] = Query(None, description="Filter by dataset"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    admin: dict = Depends(require_admin)
):
    """Get examples where user disagreed with consensus."""
    with get_db() as conn:
        # Verify user exists
        user = conn.execute(
            "SELECT id, username, display_name FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if not user:
            raise HTTPException(404, f"User not found: {user_id}")

        # Build query
        dataset_filter = "AND e.dataset = ?" if dataset else ""
        dataset_params = [dataset] if dataset else []

        # Get user's annotations
        query = f"""
            SELECT
                e.id as example_id,
                e.premise,
                e.hypothesis,
                e.dataset,
                GROUP_CONCAT(DISTINCT sl.label) as user_labels
            FROM complexity_scores cs
            JOIN examples e ON cs.example_id = e.id
            LEFT JOIN span_labels sl ON sl.example_id = e.id AND sl.annotator_id = cs.annotator_id
            WHERE cs.annotator_id = ?
            {dataset_filter}
            GROUP BY e.id
        """

        annotations = conn.execute(
            query, [user_id] + dataset_params
        ).fetchall()

        # Find disagreements
        results = []
        for ann in annotations:
            example_id = ann["example_id"]

            # Get consensus labels
            consensus_labels = conn.execute("""
                SELECT label, COUNT(*) as count
                FROM span_labels
                WHERE example_id = ?
                GROUP BY label
                HAVING count >= ?
            """, (example_id, CONSENSUS_THRESHOLD // 2 + 1)).fetchall()

            if not consensus_labels:
                continue  # No consensus yet

            consensus_label_set = set(row["label"] for row in consensus_labels)
            user_label_set = set(ann["user_labels"].split(",")) if ann["user_labels"] else set()

            # Calculate disagreement
            missing_labels = consensus_label_set - user_label_set
            extra_labels = user_label_set - consensus_label_set
            disagreement_severity = len(missing_labels) + len(extra_labels)

            if disagreement_severity > 0:
                results.append({
                    "example_id": example_id,
                    "premise": ann["premise"][:200] + "..." if len(ann["premise"]) > 200 else ann["premise"],
                    "hypothesis": ann["hypothesis"][:200] + "..." if len(ann["hypothesis"]) > 200 else ann["hypothesis"],
                    "dataset": ann["dataset"],
                    "user_labels": list(user_label_set),
                    "consensus_labels": list(consensus_label_set),
                    "missing_labels": list(missing_labels),
                    "extra_labels": list(extra_labels),
                    "disagreement_severity": disagreement_severity
                })

        # Sort by severity
        results.sort(key=lambda x: x["disagreement_severity"], reverse=True)

        return {
            "user": {
                "id": user["id"],
                "username": user["username"],
                "display_name": user["display_name"]
            },
            "disagreements": results[:limit],
            "total": len(results)
        }


# ============================================================================
# API Endpoints - Flagging
# ============================================================================

@app.post(
    "/api/flag",
    tags=["Annotation"],
    summary="Flag an example for review",
    description="Flag an example as controversial or needing review. Any authenticated user can flag.",
)
async def flag_example(
    example_id: str = Body(..., embed=True, description="Example ID to flag"),
    reason: str = Body(..., embed=True, description="Reason for flagging"),
    user: dict = Depends(get_current_user)
):
    """Flag an example for review."""
    with get_db() as conn:
        # Verify example exists
        example = conn.execute(
            "SELECT id FROM examples WHERE id = ?", (example_id,)
        ).fetchone()

        if not example:
            raise HTTPException(404, f"Example not found: {example_id}")

        # Check if already flagged by this user
        existing = conn.execute(
            "SELECT id FROM flagged_examples WHERE example_id = ? AND flagged_by = ?",
            (example_id, user["id"])
        ).fetchone()

        if existing:
            raise HTTPException(400, "You have already flagged this example")

        # Insert flag
        conn.execute("""
            INSERT INTO flagged_examples (example_id, flagged_by, flag_source, flag_reason)
            VALUES (?, ?, 'manual', ?)
        """, (example_id, user["id"], reason))

        return {"status": "flagged", "example_id": example_id}


@app.get(
    "/api/admin/flagged",
    tags=["Admin"],
    summary="Get flagged examples (admin)",
    description="Get list of flagged examples with filtering options.",
)
async def admin_get_flagged_examples(
    status: Optional[str] = Query(None, description="Filter by status: pending, reviewed, resolved, excluded"),
    source: Optional[str] = Query(None, description="Filter by source: manual, auto"),
    dataset: Optional[str] = Query(None, description="Filter by dataset"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    admin: dict = Depends(require_admin)
):
    """Get flagged examples with filters."""
    with get_db() as conn:
        # Build query
        conditions = []
        params = []

        if status:
            conditions.append("f.status = ?")
            params.append(status)

        if source:
            conditions.append("f.flag_source = ?")
            params.append(source)

        if dataset:
            conditions.append("e.dataset = ?")
            params.append(dataset)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Get total count
        total = conn.execute(f"""
            SELECT COUNT(*) FROM flagged_examples f
            JOIN examples e ON f.example_id = e.id
            WHERE {where_clause}
        """, params).fetchone()[0]

        # Get flagged examples
        results = conn.execute(f"""
            SELECT f.id, f.example_id, f.flagged_by, f.flag_source, f.flag_reason,
                   f.agreement_score, f.flagged_at, f.status, f.resolution,
                   f.resolved_by, f.resolved_at,
                   e.premise, e.hypothesis, e.dataset,
                   u.username as flagger_username
            FROM flagged_examples f
            JOIN examples e ON f.example_id = e.id
            LEFT JOIN users u ON f.flagged_by = u.id
            WHERE {where_clause}
            ORDER BY f.flagged_at DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()

        # Get summary stats
        stats = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'reviewed' THEN 1 ELSE 0 END) as reviewed,
                SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) as resolved,
                SUM(CASE WHEN status = 'excluded' THEN 1 ELSE 0 END) as excluded,
                SUM(CASE WHEN flag_source = 'auto' THEN 1 ELSE 0 END) as auto_flagged,
                SUM(CASE WHEN flag_source = 'manual' THEN 1 ELSE 0 END) as manual_flagged
            FROM flagged_examples
        """).fetchone()

        return {
            "flagged": [dict(row) for row in results],
            "total": total,
            "limit": limit,
            "offset": offset,
            "stats": {
                "total": stats["total"] or 0,
                "pending": stats["pending"] or 0,
                "reviewed": stats["reviewed"] or 0,
                "resolved": stats["resolved"] or 0,
                "excluded": stats["excluded"] or 0,
                "auto_flagged": stats["auto_flagged"] or 0,
                "manual_flagged": stats["manual_flagged"] or 0
            }
        }


@app.get(
    "/api/admin/flagged/{flag_id}",
    tags=["Admin"],
    summary="Get single flagged example details (admin)",
    description="Get detailed information about a specific flagged example.",
)
async def admin_get_flagged_example(
    flag_id: int,
    admin: dict = Depends(require_admin)
):
    """Get details of a single flagged example."""
    with get_db() as conn:
        flag = conn.execute("""
            SELECT f.*, e.premise, e.hypothesis, e.dataset,
                   u1.username as flagger_username,
                   u2.username as resolver_username
            FROM flagged_examples f
            JOIN examples e ON f.example_id = e.id
            LEFT JOIN users u1 ON f.flagged_by = u1.id
            LEFT JOIN users u2 ON f.resolved_by = u2.id
            WHERE f.id = ?
        """, (flag_id,)).fetchone()

        if not flag:
            raise HTTPException(404, f"Flagged example not found: {flag_id}")

        # Get all annotations for this example
        annotations = conn.execute("""
            SELECT u.username, u.display_name,
                   GROUP_CONCAT(DISTINCT l.label_name) as labels,
                   cs.reasoning, cs.creativity, cs.domain_knowledge,
                   cs.contextual, cs.constraints, cs.ambiguity
            FROM complexity_scores cs
            JOIN users u ON cs.annotator_id = u.id
            LEFT JOIN labels l ON l.example_id = cs.example_id AND l.annotator_id = cs.annotator_id
            WHERE cs.example_id = ?
            GROUP BY cs.id
        """, (flag["example_id"],)).fetchall()

        return {
            "flag": dict(flag),
            "annotations": [dict(a) for a in annotations]
        }


@app.put(
    "/api/admin/flagged/{flag_id}",
    tags=["Admin"],
    summary="Update flagged example status (admin)",
    description="Update the status and resolution of a flagged example.",
)
async def admin_update_flagged_example(
    flag_id: int,
    status: str = Body(..., embed=True, description="New status: pending, reviewed, resolved, excluded"),
    resolution: Optional[str] = Body(None, embed=True, description="Resolution notes"),
    admin: dict = Depends(require_admin)
):
    """Update status of a flagged example."""
    valid_statuses = ["pending", "reviewed", "resolved", "excluded"]
    if status not in valid_statuses:
        raise HTTPException(400, f"Invalid status. Must be one of: {valid_statuses}")

    with get_db() as conn:
        # Check flag exists
        flag = conn.execute(
            "SELECT id FROM flagged_examples WHERE id = ?", (flag_id,)
        ).fetchone()

        if not flag:
            raise HTTPException(404, f"Flagged example not found: {flag_id}")

        # Update status
        if status in ["resolved", "excluded"]:
            conn.execute("""
                UPDATE flagged_examples
                SET status = ?, resolution = ?, resolved_by = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, resolution, admin["id"], flag_id))
        else:
            conn.execute("""
                UPDATE flagged_examples
                SET status = ?, resolution = ?
                WHERE id = ?
            """, (status, resolution, flag_id))

        return {"status": "updated", "flag_id": flag_id, "new_status": status}


def auto_flag_controversial(conn, example_id: str, agreement_score: float):
    """Auto-flag an example if agreement is below threshold."""
    if agreement_score >= CONTROVERSIAL_THRESHOLD:
        return False

    # Check if already auto-flagged
    existing = conn.execute(
        "SELECT id FROM flagged_examples WHERE example_id = ? AND flag_source = 'auto'",
        (example_id,)
    ).fetchone()

    if existing:
        return False  # Already flagged

    # Auto-flag
    reason = f"Low agreement score: {agreement_score:.2f} (threshold: {CONTROVERSIAL_THRESHOLD})"
    conn.execute("""
        INSERT INTO flagged_examples (example_id, flag_source, flag_reason, agreement_score)
        VALUES (?, 'auto', ?, ?)
    """, (example_id, reason, agreement_score))

    return True


# ============================================================================
# API Endpoints - System
# ============================================================================

@app.get(
    "/api/tokenizer/info",
    tags=["System"],
    summary="Get tokenizer info",
    description="Get information about the tokenizer used for text segmentation. The tokenizer matches the target model (ModernBERT by default).",
)
async def tokenizer_info():
    """Get information about the tokenizer being used."""
    tokenizer = get_tokenizer()
    return {
        "model": TOKENIZER_MODEL,
        "type": type(tokenizer).__name__,
        "vocab_size": tokenizer.vocab_size,
    }


@app.get(
    "/api/labels",
    tags=["System"],
    summary="Get label schema",
    description="Get the label schema configuration including system-defined labels and custom label policy.",
    response_model=LabelSchemaResponse,
)
async def get_labels():
    """Get the label schema configuration."""
    schema = get_label_schema()
    return LabelSchemaResponse(
        categories=schema["categories"],
        system_labels=schema["system_labels"],
        allow_custom=schema["allow_custom"],
        custom_tracking=schema["custom_tracking"],
    )


@app.get(
    "/api/datasets",
    tags=["System"],
    summary="List available datasets",
    description="List all available NLI datasets. Datasets are detected from JSONL files in the data/nli directory.",
)
async def list_datasets():
    """List available datasets."""
    files = list(DATA_DIR.glob("*.jsonl"))
    datasets = set()
    for f in files:
        # Extract dataset name from filename
        name = f.stem.split("_")[0]
        datasets.add(name)
    return {"datasets": sorted(datasets)}


# Label text to integer mapping
LABEL_MAP = {
    "entailment": 0,
    "neutral": 1,
    "contradiction": 2,
    "e": 0,
    "n": 1,
    "c": 2,
    "0": 0,
    "1": 1,
    "2": 2,
}


@app.post(
    "/api/import",
    tags=["Admin"],
    summary="Bulk import examples",
    description="Import multiple NLI examples at once. Requires admin role. Accepts JSON array of examples.",
    response_model=ImportResponse,
)
@limiter.limit(RATE_LIMIT_AUTH)  # Strict limit for admin import
async def bulk_import_examples(
    http_request: Request,
    request: ImportRequest,
    admin: dict = Depends(require_admin),
):
    """Bulk import NLI examples into the database."""
    imported = 0
    skipped = 0
    errors = 0

    with get_db() as conn:
        for i, ex in enumerate(request.examples):
            try:
                # Generate ID if not provided
                example_id = ex.id or f"{request.dataset}_{i}_{secrets.token_hex(4)}"

                # Parse label
                label_int = None
                label_text = None
                if ex.label:
                    label_lower = ex.label.lower().strip()
                    label_int = LABEL_MAP.get(label_lower)
                    if label_int is not None:
                        label_text = ["entailment", "neutral", "contradiction"][label_int]
                    else:
                        # Try to parse as integer directly
                        try:
                            label_int = int(ex.label)
                            if 0 <= label_int <= 2:
                                label_text = ["entailment", "neutral", "contradiction"][label_int]
                            else:
                                label_int = None
                        except ValueError:
                            pass

                # Insert into database
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO examples
                    (id, dataset, premise, hypothesis, gold_label, gold_label_text, source_file, pool_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'zero_entry')
                """, (
                    example_id,
                    request.dataset,
                    ex.premise,
                    ex.hypothesis,
                    label_int,
                    label_text,
                    ex.source or "api_import"
                ))

                if cursor.rowcount > 0:
                    imported += 1
                else:
                    skipped += 1

            except Exception as e:
                errors += 1
                print(f"Error importing example {i}: {e}")

    return ImportResponse(
        imported=imported,
        skipped=skipped,
        errors=errors,
        dataset=request.dataset
    )


@app.post(
    "/api/import/jsonl",
    tags=["Admin"],
    summary="Import from JSONL content",
    description="Import examples from JSONL format (one JSON object per line). Requires admin role.",
    response_model=ImportResponse,
)
@limiter.limit(RATE_LIMIT_AUTH)  # Strict limit for admin import
async def import_jsonl(
    request: Request,
    content: str = Body(..., media_type="text/plain", description="JSONL content (one JSON object per line)"),
    dataset: str = Query("imported", description="Dataset name for imported examples"),
    skip_duplicates: bool = Query(True, description="Skip duplicates instead of erroring"),
    admin: dict = Depends(require_admin),
):
    """Import examples from JSONL format."""
    imported = 0
    skipped = 0
    errors = 0

    with get_db() as conn:
        for i, line in enumerate(content.strip().split("\n")):
            if not line.strip():
                continue
            try:
                ex = json.loads(line)

                # Generate ID if not provided
                example_id = ex.get("id") or f"{dataset}_{i}_{secrets.token_hex(4)}"

                # Parse label
                label_raw = ex.get("label") or ex.get("gold_label")
                label_int = None
                label_text = None
                if label_raw is not None:
                    if isinstance(label_raw, int):
                        label_int = label_raw
                        if 0 <= label_int <= 2:
                            label_text = ["entailment", "neutral", "contradiction"][label_int]
                    else:
                        label_lower = str(label_raw).lower().strip()
                        label_int = LABEL_MAP.get(label_lower)
                        if label_int is not None:
                            label_text = ["entailment", "neutral", "contradiction"][label_int]

                # Get premise/hypothesis (handle various field names)
                premise = ex.get("premise") or ex.get("sentence1") or ""
                hypothesis = ex.get("hypothesis") or ex.get("sentence2") or ""

                if not premise or not hypothesis:
                    errors += 1
                    continue

                # Insert into database
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO examples
                    (id, dataset, premise, hypothesis, gold_label, gold_label_text, source_file, pool_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'zero_entry')
                """, (
                    example_id,
                    dataset,
                    premise,
                    hypothesis,
                    label_int,
                    label_text,
                    ex.get("source") or "jsonl_import"
                ))

                if cursor.rowcount > 0:
                    imported += 1
                else:
                    skipped += 1

            except json.JSONDecodeError as e:
                errors += 1
                print(f"JSON parse error on line {i}: {e}")
            except Exception as e:
                errors += 1
                print(f"Error importing line {i}: {e}")

    return ImportResponse(
        imported=imported,
        skipped=skipped,
        errors=errors,
        dataset=dataset
    )


# ============================================================================
# API Endpoints - Locking
# ============================================================================

@app.get(
    "/api/lock/status/{example_id}",
    tags=["Locking"],
    summary="Get lock status",
    description="Check if an example is currently locked and by whom.",
    response_model=LockStatusResponse,
)
async def get_example_lock_status(
    example_id: str,
    user: dict = Depends(get_current_user)
):
    """Get the lock status for an example."""
    user_id = user["id"]
    
    with get_db() as conn:
        status = get_lock_status(conn, example_id)
        
        if status:
            return LockStatusResponse(
                example_id=example_id,
                locked=True,
                locked_by=status["locked_by"],
                locked_by_username=status["locked_by_username"],
                locked_until=status["locked_until"],
                expires_in_seconds=status["expires_in_seconds"],
                is_own_lock=status["locked_by"] == user_id
            )
        else:
            return LockStatusResponse(
                example_id=example_id,
                locked=False,
                is_own_lock=False
            )


@app.post(
    "/api/lock/release/{example_id}",
    tags=["Locking"],
    summary="Release lock",
    description="Release your lock on an example. Only the lock owner can release it.",
)
async def release_example_lock(
    example_id: str,
    user: dict = Depends(get_current_user)
):
    """Release a lock on an example."""
    user_id = user["id"]
    
    with get_db() as conn:
        released = release_lock(conn, example_id, user_id)
        
        if released:
            return {"status": "released", "example_id": example_id}
        else:
            # Check if it's locked by someone else
            status = get_lock_status(conn, example_id)
            if status:
                raise HTTPException(403, f"Example is locked by {status['locked_by_username']}")
            else:
                return {"status": "not_locked", "example_id": example_id}


@app.post(
    "/api/lock/extend/{example_id}",
    tags=["Locking"],
    summary="Extend lock",
    description="Extend your lock on an example by another 30 minutes. Only the lock owner can extend.",
)
async def extend_example_lock(
    example_id: str,
    user: dict = Depends(get_current_user)
):
    """Extend a lock on an example."""
    user_id = user["id"]
    
    with get_db() as conn:
        # Check current lock
        status = get_lock_status(conn, example_id)
        
        if not status:
            raise HTTPException(404, "No lock exists for this example")
        
        if status["locked_by"] != user_id:
            raise HTTPException(403, f"Example is locked by {status['locked_by_username']}")
        
        # Extend the lock
        lock_until = acquire_lock(conn, example_id, user_id)
        
        return {
            "status": "extended",
            "example_id": example_id,
            "lock_until": lock_until.isoformat(),
            "expires_in_seconds": LOCK_TIMEOUT_MINUTES * 60
        }


@app.get(
    "/api/lock/mine",
    tags=["Locking"],
    summary="List my locks",
    description="List all examples currently locked by the current user.",
)
async def list_my_locks(user: dict = Depends(get_current_user)):
    """List all examples locked by the current user."""
    user_id = user["id"]
    now = datetime.now()
    
    with get_db() as conn:
        # Clean up expired locks first
        cleanup_expired_locks(conn)
        
        locks = conn.execute("""
            SELECT l.example_id, l.locked_until, e.dataset, e.premise, e.hypothesis
            FROM example_locks l
            JOIN examples e ON l.example_id = e.id
            WHERE l.locked_by = ?
            ORDER BY l.created_at DESC
        """, (user_id,)).fetchall()
        
        return {
            "locks": [
                {
                    "example_id": row["example_id"],
                    "dataset": row["dataset"],
                    "premise": row["premise"][:100],  # Truncate for display
                    "hypothesis": row["hypothesis"][:100],
                    "locked_until": row["locked_until"],
                    "expires_in_seconds": int((datetime.fromisoformat(row["locked_until"]) - now).total_seconds())
                }
                for row in locks
            ],
            "count": len(locks)
        }


# ============================================================================
# API Endpoints - Agreement
# ============================================================================

@app.get(
    "/api/agreement/example/{example_id}",
    tags=["Agreement"],
    summary="Get example agreement metrics",
    description="Get inter-annotator agreement metrics for a specific example.",
    response_model=ExampleAgreementResponse,
)
async def get_example_agreement(
    example_id: str,
    user: dict = Depends(get_current_user)
):
    """Get agreement metrics for an example."""
    with get_db() as conn:
        # Get or calculate agreement
        agreement = conn.execute("""
            SELECT ea.*, e.pool_status
            FROM example_agreement ea
            JOIN examples e ON ea.example_id = e.id
            WHERE ea.example_id = ?
        """, (example_id,)).fetchone()
        
        if not agreement:
            # Calculate on-demand
            metrics = calculate_example_agreement(conn, example_id)
            pool_status = conn.execute(
                "SELECT pool_status FROM examples WHERE id = ?",
                (example_id,)
            ).fetchone()["pool_status"]
        else:
            metrics = dict(agreement)
            pool_status = agreement["pool_status"]
        
        return ExampleAgreementResponse(
            example_id=example_id,
            annotation_count=metrics['annotation_count'],
            agreement_score=metrics['agreement_score'],
            complexity_agreement=metrics['complexity_agreement'],
            span_agreement=metrics['span_agreement'],
            pool_status=pool_status,
            consensus_threshold=CONSENSUS_THRESHOLD,
            needs_more_annotations=metrics['annotation_count'] < CONSENSUS_THRESHOLD
        )


@app.get(
    "/api/agreement/annotator/{user_id}",
    tags=["Agreement"],
    summary="Get annotator agreement metrics",
    description="Get agreement metrics for a specific annotator.",
    response_model=AnnotatorAgreementResponse,
)
async def get_annotator_agreement(
    user_id: int,
    current_user: dict = Depends(get_current_user)
):
    """Get agreement metrics for an annotator."""
    with get_db() as conn:
        # Get user info
        user = conn.execute(
            "SELECT username FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        
        if not user:
            raise HTTPException(404, f"User not found: {user_id}")
        
        # Get or calculate agreement
        agreement = conn.execute("""
            SELECT * FROM annotator_agreement WHERE user_id = ?
        """, (user_id,)).fetchone()
        
        if not agreement:
            # Calculate on-demand
            metrics = calculate_annotator_agreement(conn, user_id)
        else:
            metrics = dict(agreement)
        
        return AnnotatorAgreementResponse(
            user_id=user_id,
            username=user["username"],
            total_annotations=metrics['total_annotations'],
            agreement_with_consensus=metrics['agreement_with_consensus'],
            complexity_agreement=metrics['complexity_agreement'],
            span_agreement=metrics['span_agreement'],
            last_calculated=agreement["last_calculated"] if agreement else None
        )


@app.post(
    "/api/agreement/recalculate",
    tags=["Agreement"],
    summary="Recalculate all agreement metrics",
    description="Trigger recalculation of agreement metrics for all examples and annotators. Admin only.",
)
async def recalculate_all_agreement(admin: dict = Depends(require_admin)):
    """Recalculate agreement for all examples and annotators."""
    with get_db() as conn:
        # Get all examples with multiple annotations
        examples = conn.execute("""
            SELECT example_id, COUNT(*) as count
            FROM complexity_scores
            GROUP BY example_id
            HAVING count >= 2
        """).fetchall()
        
        # Recalculate example agreement
        for row in examples:
            update_example_agreement(conn, row["example_id"])
        
        # Get all users with annotations
        users = conn.execute("""
            SELECT DISTINCT annotator_id FROM complexity_scores
        """).fetchall()
        
        # Recalculate annotator agreement
        for row in users:
            update_annotator_agreement(conn, row["annotator_id"])
        
        return {
            "status": "completed",
            "examples_updated": len(examples),
            "annotators_updated": len(users)
        }


@app.get(
    "/api/pools/stats",
    tags=["Agreement"],
    summary="Get pool statistics",
    description="Get statistics about question pools (test, building, zero_entry).",
    response_model=PoolStatsResponse,
)
async def get_pool_stats(user: dict = Depends(get_current_user)):
    """Get statistics about question pools."""
    with get_db() as conn:
        stats = conn.execute("""
            SELECT pool_status, COUNT(*) as count
            FROM examples
            GROUP BY pool_status
        """).fetchall()
        
        pool_counts = {row["pool_status"]: row["count"] for row in stats}
        total = sum(pool_counts.values())
        
        return PoolStatsResponse(
            test=pool_counts.get('test', 0),
            building=pool_counts.get('building', 0),
            zero_entry=pool_counts.get('zero_entry', 0),
            total=total,
            consensus_threshold=CONSENSUS_THRESHOLD,
            agreement_threshold=AGREEMENT_HIGH_THRESHOLD
        )



# ============================================================================
# API Endpoints - Reliability
# ============================================================================

@app.get(
    "/api/reliability/me",
    tags=["Reliability"],
    summary="Get my reliability score",
    description="Get the current user's reliability score and calibration status.",
    response_model=ReliabilityScoreResponse,
)
async def get_my_reliability(user: dict = Depends(get_current_user)):
    """Get the current user's reliability score."""
    user_id = user["id"]

    with get_db() as conn:
        # Get total annotations count
        total_annotations = conn.execute("""
            SELECT COUNT(*) as count FROM complexity_scores WHERE annotator_id = ?
        """, (user_id,)).fetchone()["count"]

        # Get reliability data
        reliability = conn.execute("""
            SELECT reliability_score, calibration_status, scored_annotation_count
            FROM annotator_agreement WHERE user_id = ?
        """, (user_id,)).fetchone()

        if reliability:
            return ReliabilityScoreResponse(
                user_id=user_id,
                username=user["username"],
                display_name=user.get("display_name"),
                reliability_score=reliability["reliability_score"],
                calibration_status=reliability["calibration_status"],
                scored_annotation_count=reliability["scored_annotation_count"],
                total_annotations=total_annotations,
                calibration_threshold=CALIBRATION_MIN_SCORED
            )
        else:
            # No reliability data yet
            return ReliabilityScoreResponse(
                user_id=user_id,
                username=user["username"],
                display_name=user.get("display_name"),
                reliability_score=None,
                calibration_status="provisional",
                scored_annotation_count=0,
                total_annotations=total_annotations,
                calibration_threshold=CALIBRATION_MIN_SCORED
            )


@app.get(
    "/api/reliability/user/{user_id}",
    tags=["Reliability"],
    summary="Get user reliability score",
    description="Get reliability score and calibration status for a specific user.",
    response_model=ReliabilityScoreResponse,
)
async def get_user_reliability(
    user_id: int,
    current_user: dict = Depends(get_current_user)
):
    """Get reliability score for a specific user."""
    with get_db() as conn:
        # Get user info
        user = conn.execute(
            "SELECT id, username, display_name FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if not user:
            raise HTTPException(404, f"User not found: {user_id}")

        # Get total annotations count
        total_annotations = conn.execute("""
            SELECT COUNT(*) as count FROM complexity_scores WHERE annotator_id = ?
        """, (user_id,)).fetchone()["count"]

        # Get reliability data
        reliability = conn.execute("""
            SELECT reliability_score, calibration_status, scored_annotation_count
            FROM annotator_agreement WHERE user_id = ?
        """, (user_id,)).fetchone()

        if reliability:
            return ReliabilityScoreResponse(
                user_id=user_id,
                username=user["username"],
                display_name=user["display_name"],
                reliability_score=reliability["reliability_score"],
                calibration_status=reliability["calibration_status"],
                scored_annotation_count=reliability["scored_annotation_count"],
                total_annotations=total_annotations,
                calibration_threshold=CALIBRATION_MIN_SCORED
            )
        else:
            return ReliabilityScoreResponse(
                user_id=user_id,
                username=user["username"],
                display_name=user["display_name"],
                reliability_score=None,
                calibration_status="provisional",
                scored_annotation_count=0,
                total_annotations=total_annotations,
                calibration_threshold=CALIBRATION_MIN_SCORED
            )


@app.get(
    "/api/reliability/leaderboard",
    tags=["Reliability"],
    summary="Get leaderboard with reliability scores",
    description="Get the annotator leaderboard including reliability scores and calibration status.",
    response_model=LeaderboardResponse,
)
async def get_reliability_leaderboard(
    sort_by: str = Query("annotations", description="Sort by: 'annotations', 'reliability', or 'username'"),
    current_user: dict = Depends(get_current_user)
):
    """Get the leaderboard with reliability scores."""
    user_id = current_user["id"]

    with get_db() as conn:
        # Get all annotators with their stats
        annotators = conn.execute("""
            SELECT u.id, u.username, u.display_name,
                   COUNT(DISTINCT c.example_id) as annotations,
                   aa.reliability_score,
                   COALESCE(aa.calibration_status, 'provisional') as calibration_status,
                   COALESCE(aa.scored_annotation_count, 0) as scored_annotation_count
            FROM users u
            LEFT JOIN complexity_scores c ON u.id = c.annotator_id
            LEFT JOIN annotator_agreement aa ON u.id = aa.user_id
            WHERE u.id > 2  -- Exclude system users (anonymous, legacy)
            GROUP BY u.id
            HAVING annotations > 0
            ORDER BY
                CASE ?
                    WHEN 'reliability' THEN -COALESCE(aa.reliability_score, -1)
                    WHEN 'username' THEN 0
                    ELSE -annotations
                END,
                CASE ? WHEN 'username' THEN u.username ELSE '' END,
                u.username
        """, (sort_by, sort_by)).fetchall()

        # Build leaderboard entries
        entries = []
        for idx, row in enumerate(annotators, 1):
            entries.append(LeaderboardEntry(
                rank=idx,
                user_id=row["id"],
                username=row["username"],
                display_name=row["display_name"],
                annotations=row["annotations"],
                reliability_score=row["reliability_score"],
                calibration_status=row["calibration_status"],
                is_current_user=(row["id"] == user_id)
            ))

        return LeaderboardResponse(
            entries=entries,
            total_annotators=len(entries),
            calibration_threshold=CALIBRATION_MIN_SCORED
        )


# ============================================================================
# API Endpoints - Examples
# ============================================================================

@app.get(
    "/api/next",
    tags=["Examples"],
    summary="Get next example to annotate",
    description="""Get the next unannotated example for the current user. Automatically locks the example.

**Calibration Routing:**
- New/provisional users are routed to "test" pool questions (high consensus) for calibration
- Calibrated users get normal routing with occasional test questions for drift detection
- Routing is stealth - users don't know which questions are calibration
- Configurable via CALIBRATION_TEST_RATIO_NEW and CALIBRATION_TEST_RATIO_ESTABLISHED

**Pool Priority (when not serving test question):**
1. zero_entry - Fresh examples, no annotations yet
2. building - Being actively annotated, building consensus
3. test - High consensus calibration questions (served based on calibration ratio)
""",
    response_model=ExampleResponse,
)
@limiter.limit(RATE_LIMIT_ANNOTATION)
async def get_next_example(
    request: Request,
    dataset: str = Query(None, description="Filter by dataset (snli, mnli, anli, etc.)"),
    gold_label: str = Query(None, description="Filter by gold label (entailment, neutral, contradiction)"),
    user: dict = Depends(get_current_user)
):
    """Get the next example to annotate with calibration-aware routing."""
    user_id = user["id"]

    # Convert gold_label text to numeric if provided
    gold_label_num = None
    if gold_label:
        label_map = {"entailment": 0, "e": 0, "neutral": 1, "n": 1, "contradiction": 2, "c": 2}
        gold_label_num = label_map.get(gold_label.lower())
        if gold_label_num is None:
            raise HTTPException(400, f"Invalid gold_label: {gold_label}. Use entailment/neutral/contradiction")

    with get_db() as conn:
        # Clean up expired locks
        cleanup_expired_locks(conn)

        # Get user's calibration status and test question ratio
        calibration_status, test_ratio = get_user_calibration_status(conn, user_id)
        serve_test = should_serve_test_question(test_ratio)

        # Base exclusion criteria (always apply)
        base_exclusions = """
            e.id NOT IN (
                SELECT example_id FROM complexity_scores WHERE annotator_id = ?
            )
            AND e.id NOT IN (
                SELECT example_id FROM skipped WHERE annotator_id = ?
            )
            AND e.id NOT IN (
                SELECT example_id FROM example_locks
                WHERE locked_by != ? AND locked_until > datetime('now')
            )
        """

        row = None

        if serve_test:
            # Try to serve a test question (high consensus, for calibration)
            query = f"""
                SELECT e.*, ea.annotation_count
                FROM examples e
                LEFT JOIN example_agreement ea ON e.id = ea.example_id
                WHERE {base_exclusions}
                AND e.pool_status = 'test'
            """
            params = [user_id, user_id, user_id]

            if dataset:
                query += " AND e.dataset = ?"
                params.append(dataset)

            if gold_label_num is not None:
                query += " AND e.gold_label = ?"
                params.append(gold_label_num)

            query += " ORDER BY RANDOM() LIMIT 1"
            row = conn.execute(query, params).fetchone()

        if row is None:
            # Fallback to normal routing: zero_entry > building > test
            query = f"""
                SELECT e.*, ea.annotation_count,
                       CASE e.pool_status
                           WHEN 'zero_entry' THEN 1
                           WHEN 'building' THEN 2
                           WHEN 'test' THEN 3
                           ELSE 4
                       END as pool_priority
                FROM examples e
                LEFT JOIN example_agreement ea ON e.id = ea.example_id
                WHERE {base_exclusions}
            """
            params = [user_id, user_id, user_id]

            if dataset:
                query += " AND e.dataset = ?"
                params.append(dataset)

            if gold_label_num is not None:
                query += " AND e.gold_label = ?"
                params.append(gold_label_num)

            query += " ORDER BY pool_priority, RANDOM() LIMIT 1"
            row = conn.execute(query, params).fetchone()

        if not row:
            raise HTTPException(404, "No more examples to annotate")

        # Acquire lock
        lock_until = acquire_lock(conn, row["id"], user_id)

        # Get existing labels (shouldn't exist for next, but check anyway)
        existing_labels = []
        existing_scores = None

        # Tokenize the texts
        premise_words = tokenize_text(row["premise"])
        hypothesis_words = tokenize_text(row["hypothesis"])

        # Compute/retrieve auto-spans (Tier 0 labels)
        auto_spans = ensure_auto_spans_computed(conn, row["id"], row["premise"], row["hypothesis"])

        return ExampleResponse(
            id=row["id"],
            dataset=row["dataset"],
            premise=row["premise"],
            hypothesis=row["hypothesis"],
            gold_label=row["gold_label"],
            gold_label_text=row["gold_label_text"],
            premise_words=premise_words,
            hypothesis_words=hypothesis_words,
            existing_labels=existing_labels,
            existing_scores=existing_scores,
            lock_until=lock_until.isoformat() if lock_until else None,
            pool_status=row["pool_status"],
            auto_spans=auto_spans
        )


@app.get(
    "/api/example/{example_id}",
    tags=["Examples"],
    summary="Get specific example",
    description="Get a specific example by ID with tokenized text and existing annotations.",
    response_model=ExampleResponse,
)
async def get_example(
    example_id: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific example by ID."""
    user_id = user["id"]
    
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM examples WHERE id = ?",
            (example_id,)
        ).fetchone()
        
        if not row:
            raise HTTPException(404, f"Example not found: {example_id}")
        
        # Get existing labels for this user
        labels = conn.execute("""
            SELECT l.label_name, l.label_color, l.is_custom,
                   s.source, s.word_index, s.word_text, s.char_start, s.char_end
            FROM labels l
            JOIN span_selections s ON s.label_id = l.id
            WHERE l.example_id = ? AND l.annotator_id = ?
        """, (example_id, user_id)).fetchall()
        
        # Group spans by label
        label_dict = {}
        for label_row in labels:
            name = label_row["label_name"]
            if name not in label_dict:
                label_dict[name] = {
                    "label_name": name,
                    "label_color": label_row["label_color"],
                    "is_custom": bool(label_row["is_custom"]),
                    "spans": []
                }
            label_dict[name]["spans"].append({
                "source": label_row["source"],
                "word_index": label_row["word_index"],
                "word_text": label_row["word_text"],
                "char_start": label_row["char_start"],
                "char_end": label_row["char_end"]
            })
        
        existing_labels = list(label_dict.values())
        
        # Get existing complexity scores
        scores = conn.execute("""
            SELECT reasoning, creativity, domain_knowledge, contextual, constraints, ambiguity
            FROM complexity_scores
            WHERE example_id = ? AND annotator_id = ?
        """, (example_id, user_id)).fetchone()
        
        existing_scores = dict(scores) if scores else None
        
        # Get lock status
        lock_status = get_lock_status(conn, example_id)
        lock_until = lock_status["locked_until"] if lock_status else None
        
        # Tokenize the texts
        premise_words = tokenize_text(row["premise"])
        hypothesis_words = tokenize_text(row["hypothesis"])

        # Compute/retrieve auto-spans (Tier 0 labels)
        auto_spans = ensure_auto_spans_computed(conn, row["id"], row["premise"], row["hypothesis"])

        return ExampleResponse(
            id=row["id"],
            dataset=row["dataset"],
            premise=row["premise"],
            hypothesis=row["hypothesis"],
            gold_label=row["gold_label"],
            gold_label_text=row["gold_label_text"],
            premise_words=premise_words,
            hypothesis_words=hypothesis_words,
            existing_labels=existing_labels,
            existing_scores=existing_scores,
            lock_until=lock_until,
            pool_status=row["pool_status"],
            auto_spans=auto_spans
        )


# ============================================================================
# API Endpoints - Annotation
# ============================================================================

@app.post(
    "/api/label",
    tags=["Annotation"],
    summary="Submit annotation",
    description="Submit span labels and complexity scores for an example. Automatically releases any lock. Updates agreement metrics.",
)
@limiter.limit(RATE_LIMIT_ANNOTATION)
async def submit_annotation(
    request: Request,
    submission: AnnotationSubmission,
    user: dict = Depends(get_current_user)
):
    """Submit labels and scores for an example."""
    user_id = user["id"]
    custom_labels_used = []

    with get_db() as conn:
        # Verify example exists
        example = conn.execute(
            "SELECT id FROM examples WHERE id = ?", (submission.example_id,)
        ).fetchone()
        if not example:
            raise HTTPException(404, f"Example not found: {submission.example_id}")

        # Delete existing labels for this user+example
        conn.execute(
            "DELETE FROM labels WHERE example_id = ? AND annotator_id = ?",
            (submission.example_id, user_id)
        )
        
        # Save new labels with is_custom flag
        for label in submission.labels:
            if not label.spans:
                continue  # Skip labels with no spans

            # Check if this is a custom label
            is_custom = not is_system_label(label.label_name)
            if is_custom:
                custom_labels_used.append(label.label_name)

            cursor = conn.execute("""
                INSERT INTO labels (example_id, annotator_id, label_name, label_color, is_custom)
                VALUES (?, ?, ?, ?, ?)
            """, (submission.example_id, user_id, label.label_name, label.label_color, is_custom))
            label_id = cursor.lastrowid

            # Save span selections
            for span in label.spans:
                conn.execute("""
                    INSERT INTO span_selections
                    (label_id, source, word_index, word_text, char_start, char_end)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    label_id,
                    span.source,
                    span.word_index,
                    span.word_text,
                    span.char_start,
                    span.char_end
                ))

        # Save complexity scores if provided
        if submission.complexity_scores:
            conn.execute("""
                INSERT OR REPLACE INTO complexity_scores
                (example_id, annotator_id, reasoning, creativity, domain_knowledge, 
                 contextual, constraints, ambiguity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                submission.example_id,
                user_id,
                submission.complexity_scores.get("reasoning"),
                submission.complexity_scores.get("creativity"),
                submission.complexity_scores.get("domain_knowledge"),
                submission.complexity_scores.get("contextual"),
                submission.complexity_scores.get("constraints"),
                submission.complexity_scores.get("ambiguity"),
            ))

        # Release the lock (if any)
        release_lock(conn, submission.example_id, user_id)
        
        # Update agreement metrics for this example
        agreement_metrics = update_example_agreement(conn, submission.example_id)

        response = {
            "status": "saved", 
            "example_id": submission.example_id, 
            "annotator_id": user_id,
            "lock_released": True,
            "agreement": {
                "annotation_count": agreement_metrics['annotation_count'],
                "agreement_score": agreement_metrics['agreement_score'],
            }
        }
        
        if custom_labels_used:
            response["custom_labels"] = custom_labels_used
            response["note"] = f"Custom labels used: {', '.join(custom_labels_used)}. These are tracked separately from system labels."
        
        return response


@app.post(
    "/api/skip/{example_id}",
    tags=["Annotation"],
    summary="Skip example",
    description="Mark an example as skipped. Skipped examples won't appear in /api/next for the current user. Releases any lock on the example.",
)
@limiter.limit(RATE_LIMIT_ANNOTATION)
async def skip_example(
    request: Request,
    example_id: str,
    user: dict = Depends(get_current_user)
):
    """Mark an example as skipped by the current user."""
    user_id = user["id"]

    with get_db() as conn:
        # Verify example exists
        example = conn.execute(
            "SELECT id FROM examples WHERE id = ?", (example_id,)
        ).fetchone()
        if not example:
            raise HTTPException(404, f"Example not found: {example_id}")

        conn.execute("""
            INSERT OR REPLACE INTO skipped (example_id, annotator_id)
            VALUES (?, ?)
        """, (example_id, user_id))
        
        # Release the lock (if any)
        release_lock(conn, example_id, user_id)
        
        return {"status": "skipped", "example_id": example_id, "annotator_id": user_id, "lock_released": True}


# ============================================================================
# API Endpoints - Statistics
# ============================================================================

@app.get(
    "/api/stats",
    tags=["Statistics"],
    summary="Get statistics",
    description="Get annotation statistics including per-dataset counts, label distribution, complexity score averages, per-annotator stats, custom label usage, active locks, and agreement metrics.",
)
async def get_stats(user: dict = Depends(get_optional_user)):
    """Get labeling statistics."""
    user_id = user["id"] if user else None
    
    with get_db() as conn:
        # Clean up expired locks
        cleanup_expired_locks(conn)
        
        # Total examples per dataset
        dataset_counts = conn.execute("""
            SELECT dataset, COUNT(*) as total FROM examples GROUP BY dataset
        """).fetchall()

        # Labeled examples per dataset (all users)
        labeled_counts = conn.execute("""
            SELECT e.dataset, COUNT(DISTINCT c.example_id) as labeled
            FROM examples e
            LEFT JOIN complexity_scores c ON e.id = c.example_id
            WHERE c.example_id IS NOT NULL
            GROUP BY e.dataset
        """).fetchall()

        # Skipped examples per dataset (all users)
        skipped_counts = conn.execute("""
            SELECT e.dataset, COUNT(DISTINCT s.example_id) as skipped
            FROM examples e
            LEFT JOIN skipped s ON e.id = s.example_id
            WHERE s.example_id IS NOT NULL
            GROUP BY e.dataset
        """).fetchall()

        # Total span labels
        span_stats = conn.execute("""
            SELECT
                COUNT(DISTINCT l.id) as total_labels,
                COUNT(s.id) as total_spans,
                COUNT(DISTINCT l.example_id) as examples_with_spans
            FROM labels l
            LEFT JOIN span_selections s ON s.label_id = l.id
        """).fetchone()

        # Label distribution (with custom flag)
        label_dist = conn.execute("""
            SELECT label_name, is_custom, COUNT(*) as count
            FROM labels
            GROUP BY label_name, is_custom
            ORDER BY count DESC
        """).fetchall()

        # Custom label stats
        custom_stats = conn.execute("""
            SELECT 
                SUM(CASE WHEN is_custom = 1 THEN 1 ELSE 0 END) as custom_count,
                SUM(CASE WHEN is_custom = 0 THEN 1 ELSE 0 END) as system_count,
                COUNT(DISTINCT CASE WHEN is_custom = 1 THEN label_name END) as unique_custom_labels
            FROM labels
        """).fetchone()

        # Complexity score averages
        score_avgs = conn.execute("""
            SELECT
                AVG(reasoning) as reasoning,
                AVG(creativity) as creativity,
                AVG(domain_knowledge) as domain_knowledge,
                AVG(contextual) as contextual,
                AVG(constraints) as constraints,
                AVG(ambiguity) as ambiguity,
                COUNT(*) as total
            FROM complexity_scores
        """).fetchone()

        # Annotator stats
        annotator_stats = conn.execute("""
            SELECT u.username, u.display_name, u.role, COUNT(DISTINCT c.example_id) as labeled
            FROM users u
            LEFT JOIN complexity_scores c ON u.id = c.annotator_id
            GROUP BY u.id
            HAVING labeled > 0
            ORDER BY labeled DESC
        """).fetchall()

        # Active locks stats
        lock_stats = conn.execute("""
            SELECT COUNT(*) as active_locks,
                   COUNT(DISTINCT locked_by) as users_with_locks
            FROM example_locks
        """).fetchone()

        # Pool stats
        pool_stats = conn.execute("""
            SELECT pool_status, COUNT(*) as count
            FROM examples
            GROUP BY pool_status
        """).fetchall()

        # Agreement stats
        agreement_stats = conn.execute("""
            SELECT 
                AVG(agreement_score) as avg_agreement,
                COUNT(*) as examples_with_agreement
            FROM example_agreement
            WHERE agreement_score IS NOT NULL
        """).fetchone()

        # User-specific stats
        user_stats = None
        if user_id:
            user_labeled = conn.execute("""
                SELECT COUNT(*) as count FROM complexity_scores WHERE annotator_id = ?
            """, (user_id,)).fetchone()
            user_skipped = conn.execute("""
                SELECT COUNT(*) as count FROM skipped WHERE annotator_id = ?
            """, (user_id,)).fetchone()
            user_custom = conn.execute("""
                SELECT COUNT(*) as count FROM labels WHERE annotator_id = ? AND is_custom = 1
            """, (user_id,)).fetchone()
            user_locks = conn.execute("""
                SELECT COUNT(*) as count FROM example_locks WHERE locked_by = ?
            """, (user_id,)).fetchone()
            user_agreement = conn.execute("""
                SELECT agreement_with_consensus FROM annotator_agreement WHERE user_id = ?
            """, (user_id,)).fetchone()
            
            user_stats = {
                "labeled": user_labeled["count"],
                "skipped": user_skipped["count"],
                "custom_labels_used": user_custom["count"],
                "active_locks": user_locks["count"],
                "agreement_with_consensus": user_agreement["agreement_with_consensus"] if user_agreement else None
            }

        return {
            "datasets": {
                row["dataset"]: {"total": row["total"]}
                for row in dataset_counts
            },
            "labeled": {
                row["dataset"]: row["labeled"]
                for row in labeled_counts
            },
            "skipped": {
                row["dataset"]: row["skipped"]
                for row in skipped_counts
            },
            "spans": {
                "total_labels": span_stats["total_labels"],
                "total_spans": span_stats["total_spans"],
                "examples_with_spans": span_stats["examples_with_spans"]
            },
            "label_distribution": [
                {
                    "name": row["label_name"],
                    "is_custom": bool(row["is_custom"]),
                    "count": row["count"]
                }
                for row in label_dist
            ],
            "custom_labels": {
                "total_custom": custom_stats["custom_count"],
                "total_system": custom_stats["system_count"],
                "unique_custom": custom_stats["unique_custom_labels"]
            },
            "complexity_averages": {
                "reasoning": round(score_avgs["reasoning"], 1) if score_avgs["reasoning"] else None,
                "creativity": round(score_avgs["creativity"], 1) if score_avgs["creativity"] else None,
                "domain_knowledge": round(score_avgs["domain_knowledge"], 1) if score_avgs["domain_knowledge"] else None,
                "contextual": round(score_avgs["contextual"], 1) if score_avgs["contextual"] else None,
                "constraints": round(score_avgs["constraints"], 1) if score_avgs["constraints"] else None,
                "ambiguity": round(score_avgs["ambiguity"], 1) if score_avgs["ambiguity"] else None,
                "total": score_avgs["total"]
            },
            "annotators": [
                {
                    "username": row["username"],
                    "display_name": row["display_name"],
                    "role": row["role"],
                    "labeled": row["labeled"]
                }
                for row in annotator_stats
            ],
            "locks": {
                "active": lock_stats["active_locks"],
                "users_with_locks": lock_stats["users_with_locks"]
            },
            "pools": {
                row["pool_status"]: row["count"]
                for row in pool_stats
            },
            "agreement": {
                "average_agreement": round(agreement_stats["avg_agreement"], 3) if agreement_stats["avg_agreement"] else None,
                "examples_with_agreement": agreement_stats["examples_with_agreement"]
            },
            "user_stats": user_stats
        }


# ============================================================================
# Export Helper Functions
# ============================================================================

def get_consensus_labels(conn, example_id: str) -> dict:
    """
    Calculate consensus labels from multiple annotators using majority vote.
    
    Returns dict with:
    - labels: list of consensus labels with spans (weighted by annotator reliability)
    - confidence: confidence score for each label
    """
    # Get all annotations for this example
    annotations = conn.execute("""
        SELECT l.annotator_id, l.label_name, l.label_color,
               s.source, s.word_index, s.word_text,
               aa.agreement_with_consensus as reliability
        FROM labels l
        LEFT JOIN span_selections s ON s.label_id = l.id
        LEFT JOIN annotator_agreement aa ON l.annotator_id = aa.user_id
        WHERE l.example_id = ?
    """, (example_id,)).fetchall()
    
    if not annotations:
        return {"labels": [], "confidence": {}}
    
    # Group spans by label name
    label_spans = defaultdict(lambda: defaultdict(list))
    annotator_count = len(set(row['annotator_id'] for row in annotations))
    
    for row in annotations:
        if row['source']:
            label_name = row['label_name']
            span_key = (row['source'], row['word_index'])
            reliability = row['reliability'] if row['reliability'] is not None else 0.5
            label_spans[label_name][span_key].append((row['annotator_id'], reliability))
    
    consensus_labels = []
    confidence_scores = {}
    
    for label_name, spans in label_spans.items():
        consensus_spans = []
        total_confidence = 0
        
        for span_key, annotators in spans.items():
            total_weight = sum(rel for _, rel in annotators)
            vote_count = len(annotators)
            threshold = annotator_count * 0.5
            
            if total_weight >= threshold:
                source, word_index = span_key
                word_text = next(
                    (row['word_text'] for row in annotations 
                     if row['source'] == source and row['word_index'] == word_index),
                    None
                )
                
                consensus_spans.append({
                    "source": source,
                    "word_index": word_index,
                    "word_text": word_text,
                    "support": vote_count,
                    "confidence": total_weight / annotator_count
                })
                total_confidence += total_weight / annotator_count
        
        if consensus_spans:
            label_color = next(
                (row['label_color'] for row in annotations if row['label_name'] == label_name),
                "#808080"
            )
            
            consensus_labels.append({
                "label_name": label_name,
                "label_color": label_color,
                "spans": consensus_spans
            })
            confidence_scores[label_name] = total_confidence / len(consensus_spans) if consensus_spans else 0
    
    return {"labels": consensus_labels, "confidence": confidence_scores}


def get_consensus_complexity(conn, example_id: str) -> dict:
    """
    Calculate consensus complexity scores (weighted average by annotator reliability).
    """
    scores = conn.execute("""
        SELECT c.reasoning, c.creativity, c.domain_knowledge, c.contextual, 
               c.constraints, c.ambiguity, c.annotator_id,
               aa.agreement_with_consensus as reliability
        FROM complexity_scores c
        LEFT JOIN annotator_agreement aa ON c.annotator_id = aa.user_id
        WHERE c.example_id = ?
    """, (example_id,)).fetchall()
    
    if not scores:
        return None
    
    dimensions = ['reasoning', 'creativity', 'domain_knowledge', 'contextual', 'constraints', 'ambiguity']
    consensus = {}
    
    for dim in dimensions:
        values = [(row[dim], row['reliability'] if row['reliability'] else 0.5) 
                  for row in scores if row[dim] is not None]
        
        if not values:
            consensus[dim] = None
            continue
        
        total_weight = sum(rel for _, rel in values)
        weighted_sum = sum(val * rel for val, rel in values)
        consensus[dim] = round(weighted_sum / total_weight) if total_weight > 0 else None
    
    return consensus


def deduplicate_annotations(conn, example_id: str) -> dict:
    """
    Pick single best annotation from the highest reliability annotator.
    """
    annotators = conn.execute("""
        SELECT c.annotator_id, aa.agreement_with_consensus as reliability,
               c.created_at, u.username
        FROM complexity_scores c
        LEFT JOIN annotator_agreement aa ON c.annotator_id = aa.user_id
        LEFT JOIN users u ON c.annotator_id = u.id
        WHERE c.example_id = ?
        ORDER BY aa.agreement_with_consensus DESC NULLS LAST, c.created_at DESC
        LIMIT 1
    """, (example_id,)).fetchone()
    
    if not annotators:
        return None
    
    best_annotator_id = annotators['annotator_id']
    
    labels = conn.execute("""
        SELECT l.label_name, l.label_color, l.is_custom,
               s.source, s.word_index, s.word_text
        FROM labels l
        LEFT JOIN span_selections s ON s.label_id = l.id
        WHERE l.example_id = ? AND l.annotator_id = ?
    """, (example_id, best_annotator_id)).fetchall()
    
    scores = conn.execute("""
        SELECT reasoning, creativity, domain_knowledge, contextual, constraints, ambiguity
        FROM complexity_scores
        WHERE example_id = ? AND annotator_id = ?
    """, (example_id, best_annotator_id)).fetchone()
    
    label_dict = defaultdict(lambda: {"label_name": "", "label_color": "", "is_custom": False, "spans": []})
    for row in labels:
        label_name = row['label_name']
        label_dict[label_name]['label_name'] = label_name
        label_dict[label_name]['label_color'] = row['label_color']
        label_dict[label_name]['is_custom'] = bool(row['is_custom'])
        if row['source']:
            label_dict[label_name]['spans'].append({
                "source": row['source'],
                "word_index": row['word_index'],
                "word_text": row['word_text']
            })
    
    return {
        "annotator_id": best_annotator_id,
        "annotator_username": annotators['username'],
        "reliability": annotators['reliability'],
        "labels": list(label_dict.values()),
        "complexity_scores": dict(scores) if scores else None
    }


def format_export_record(conn, example_row: dict, mode: str, include_metadata: bool = False) -> dict:
    """Format a single example record for export based on mode."""
    example_id = example_row['id']
    
    record = {
        "id": example_id,
        "dataset": example_row['dataset'],
        "premise": example_row['premise'],
        "hypothesis": example_row['hypothesis'],
        "gold_label": example_row['gold_label'],
        "gold_label_text": example_row['gold_label_text'],
    }
    
    if mode == "raw":
        annotations = []
        annotators = conn.execute("""
            SELECT DISTINCT annotator_id FROM complexity_scores WHERE example_id = ?
        """, (example_id,)).fetchall()
        
        for annotator_row in annotators:
            annotator_id = annotator_row['annotator_id']
            
            labels = conn.execute("""
                SELECT l.label_name, l.label_color, l.is_custom,
                       s.source, s.word_index, s.word_text
                FROM labels l
                LEFT JOIN span_selections s ON s.label_id = l.id
                WHERE l.example_id = ? AND l.annotator_id = ?
            """, (example_id, annotator_id)).fetchall()
            
            scores = conn.execute("""
                SELECT reasoning, creativity, domain_knowledge, contextual, constraints, ambiguity
                FROM complexity_scores WHERE example_id = ? AND annotator_id = ?
            """, (example_id, annotator_id)).fetchone()
            
            username = conn.execute("SELECT username FROM users WHERE id = ?", (annotator_id,)).fetchone()
            
            label_dict = defaultdict(lambda: {"label_name": "", "label_color": "", "is_custom": False, "spans": []})
            for row in labels:
                label_name = row['label_name']
                label_dict[label_name]['label_name'] = label_name
                label_dict[label_name]['label_color'] = row['label_color']
                label_dict[label_name]['is_custom'] = bool(row['is_custom'])
                if row['source']:
                    label_dict[label_name]['spans'].append({
                        "source": row['source'],
                        "word_index": row['word_index'],
                        "word_text": row['word_text']
                    })
            
            annotations.append({
                "annotator_id": annotator_id,
                "annotator_username": username['username'] if username else None,
                "labels": list(label_dict.values()),
                "complexity_scores": dict(scores) if scores else None
            })
        
        record["annotations"] = annotations
        
    elif mode == "deduplicated":
        best = deduplicate_annotations(conn, example_id)
        if best:
            record["labels"] = best["labels"]
            record["complexity_scores"] = best["complexity_scores"]
            if include_metadata:
                record["selected_annotator"] = {
                    "id": best["annotator_id"],
                    "username": best["annotator_username"],
                    "reliability": best["reliability"]
                }
    
    elif mode == "consensus":
        consensus_labels = get_consensus_labels(conn, example_id)
        consensus_scores = get_consensus_complexity(conn, example_id)
        record["labels"] = consensus_labels["labels"]
        record["complexity_scores"] = consensus_scores
        if include_metadata:
            record["label_confidence"] = consensus_labels["confidence"]
    
    elif mode == "gold":
        if example_row['gold_label'] is not None:
            consensus_labels = get_consensus_labels(conn, example_id)
            consensus_scores = get_consensus_complexity(conn, example_id)
            record["labels"] = consensus_labels["labels"]
            record["complexity_scores"] = consensus_scores
            if include_metadata:
                record["label_confidence"] = consensus_labels["confidence"]
        else:
            return None
    
    if include_metadata:
        agreement = conn.execute("""
            SELECT annotation_count, agreement_score, complexity_agreement, span_agreement
            FROM example_agreement WHERE example_id = ?
        """, (example_id,)).fetchone()
        
        if agreement:
            record["metadata"] = {
                "annotation_count": agreement['annotation_count'],
                "agreement_score": agreement['agreement_score'],
                "complexity_agreement": agreement['complexity_agreement'],
                "span_agreement": agreement['span_agreement'],
                "pool_status": example_row.get('pool_status')
            }
    
    return record


def export_to_csv(records: list) -> str:
    """Convert export records to CSV format."""
    import csv
    import io
    
    if not records:
        return ""
    
    output = io.StringIO()
    flattened = []
    
    for record in records:
        flat = {
            "id": record["id"],
            "dataset": record["dataset"],
            "premise": record["premise"],
            "hypothesis": record["hypothesis"],
            "gold_label": record.get("gold_label"),
            "gold_label_text": record.get("gold_label_text"),
        }
        
        if "complexity_scores" in record and record["complexity_scores"]:
            for dim in ['reasoning', 'creativity', 'domain_knowledge', 'contextual', 'constraints', 'ambiguity']:
                flat[f"complexity_{dim}"] = record["complexity_scores"].get(dim)
        
        if "labels" in record:
            flat["label_names"] = ",".join([l["label_name"] for l in record["labels"]])
        
        if "metadata" in record:
            flat["annotation_count"] = record["metadata"]["annotation_count"]
            flat["agreement_score"] = record["metadata"].get("agreement_score")
        
        flattened.append(flat)
    
    if flattened:
        fieldnames = list(flattened[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flattened)
    
    return output.getvalue()


# ============================================================================
# API Endpoint - Export
# ============================================================================

@app.get(
    "/api/export",
    tags=["Statistics"],
    summary="Export annotations",
    description="""
Export annotation data with various modes and filters.

**Export Modes:**
- `raw`: All annotations from all annotators
- `deduplicated`: One annotation per example (highest reliability annotator)
- `consensus`: Consensus labels via weighted majority vote
- `gold`: Only gold-labeled examples with consensus annotations

**Output Formats:**
- `json`: Structured JSON format (default)
- `csv`: Flattened CSV format

**Filters:**
- `dataset`: Filter by specific dataset
- `min_agreement`: Minimum agreement score (0-1)
- `min_annotations`: Minimum annotations per example
- `include_metadata`: Add reliability and agreement metrics
- `limit`: Cap number of exported examples
    """,
)
async def export_annotations(
    mode: str = Query("raw", regex="^(raw|deduplicated|consensus|gold)$"),
    format: str = Query("json", regex="^(json|csv)$"),
    dataset: Optional[str] = Query(None),
    min_agreement: Optional[float] = Query(None, ge=0, le=1),
    min_annotations: Optional[int] = Query(None, ge=1),
    include_metadata: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1),
    user: dict = Depends(get_current_user)
):
    """Export annotation data with filtering and formatting options."""
    
    with get_db() as conn:
        query = """
            SELECT DISTINCT e.*, ea.annotation_count, ea.agreement_score
            FROM examples e
            LEFT JOIN example_agreement ea ON e.id = ea.example_id
            WHERE 1=1
        """
        params = []
        
        if dataset:
            query += " AND e.dataset = ?"
            params.append(dataset)
        
        if min_annotations:
            query += " AND ea.annotation_count >= ?"
            params.append(min_annotations)
        
        if min_agreement:
            query += " AND ea.agreement_score >= ?"
            params.append(min_agreement)
        
        if mode == "gold":
            query += " AND e.gold_label IS NOT NULL"
        
        if mode != "raw":
            query += " AND ea.annotation_count > 0"
        
        query += " ORDER BY e.id"
        
        if limit:
            query += f" LIMIT {limit}"
        
        examples = conn.execute(query, params).fetchall()
        
        records = []
        for example_row in examples:
            record = format_export_record(conn, dict(example_row), mode, include_metadata)
            if record:
                records.append(record)
        
        filters_applied = {
            "dataset": dataset,
            "min_agreement": min_agreement,
            "min_annotations": min_annotations,
            "include_metadata": include_metadata,
            "limit": limit
        }
        
        if format == "csv":
            csv_content = export_to_csv(records)
            return Response(
                content=csv_content,
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename=nli_export_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                }
            )
        else:
            return {
                "mode": mode,
                "format": format,
                "filters": filters_applied,
                "count": len(records),
                "examples": records
            }
