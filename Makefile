# Define variables
SBT_CMD = sbt "project verifTL"
TEST_CMD = testOnly verif.TLL2CacheTest -- -z
VERIF_DIR = verif
LOG_DIR = verif/logs
GOLDEN_DIR = verif/golden

# List of test names
TESTS = L2_formal_phy_hit_parrp_miss \
		L2_formal_nest_release_same_core \
		L2_formal_capacity_eviction \
		L2_formal_nest_release_diff_core \
		L2_formal_two_released_ways \
		L2_formal_shared_way \
		L2_formal_probe # \
		# L2_formal_non_coherent_request

# Generate log file paths
LOG_FILES = $(patsubst %, $(LOG_DIR)/%.log, $(TESTS))


# Run a single test case and save the log
$(LOG_DIR)/%.log:
	mkdir -p $(LOG_DIR)
	$(SBT_CMD) "$(TEST_CMD) $* -oD" 2> $@

# Compare a test log with its golden file
check-%: $(LOG_DIR)/%.log
	@mkdir -p $(GOLDEN_DIR)
	@diff -u $(GOLDEN_DIR)/$*.log $(LOG_DIR)/$*.log && echo "$* PASSED" || (echo "$* FAILED"; exit 1)


# Run all tests
.PHONY: all
all: $(LOG_FILES)

# Compare all test logs with golden files
.PHONY: check
check: $(patsubst %, check-%, $(TESTS))

# Run a specific test
.PHONY: run check-one
run: $(LOG_DIR)/$(TEST).log
check-one: check-$(TEST)

# Run all tests and check results
.PHONY: test-all
test-all: all check

# Clean logs
.PHONY: clean
clean:
	rm -rf $(LOG_DIR)
