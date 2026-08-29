#!/bin/bash
# Minimal assertion helpers shared by every scripts/tests/test_*.sh file.
# Not a framework — just enough to keep test files readable.

TESTS_RUN=0
TESTS_FAILED=0

assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" != "$actual" ]]; then
        echo "FAIL: $desc"
        echo "  expected: $expected"
        echo "  actual:   $actual"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi
}

assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        echo "FAIL: $desc"
        echo "  expected to contain: $needle"
        echo "  actual: $haystack"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi
}

assert_not_contains() {
    local desc="$1" haystack="$2" needle="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "FAIL: $desc"
        echo "  expected NOT to contain: $needle"
        echo "  actual: $haystack"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi
}

test_summary() {
    echo "$TESTS_RUN run, $TESTS_FAILED failed"
    [[ "$TESTS_FAILED" -eq 0 ]]
}
