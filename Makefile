UV ?= uv

NAME         := cli-deck
WRAPPER_SRC  := $(CURDIR)/scripts/$(NAME).bash
BASHRC_D     := $(HOME)/.bashrc.d
WRAPPER_LINK := $(BASHRC_D)/$(NAME)

.PHONY: deps install uninstall test

deps:
	$(UV) sync

# The CLI, plus the shell wrapper that exports a named bash function as
# BASH_FUNC_<name>%% so a deck child started with `bash -ic` can see it.
install:
	$(UV) tool install .
	mkdir -p $(BASHRC_D)
	ln -sfn $(WRAPPER_SRC) $(WRAPPER_LINK)
	@echo
	@echo "installed:"
	@echo "  CLI      $$(type -P $(NAME) || echo '<not on PATH - run: uv tool update-shell>')"
	@echo "  wrapper  $(WRAPPER_LINK) -> $(WRAPPER_SRC)"
	@echo
	@echo "open a new shell, or: source $(WRAPPER_LINK)"

# Reverses install. Refuses while a deck daemon is running, since that daemon
# still owns live processes; FORCE=1 overrides.
uninstall:
	@if [ -z "$(FORCE)" ] && pgrep -u "$$(id -u)" -f "cli_de[c]k --daemon" >/dev/null 2>&1; then \
		echo "$(NAME): a deck daemon is still running - stop it ('$(NAME)' then q, or q in the TUI)"; \
		echo "  ($(NAME) -l lists live decks; or force with: make uninstall FORCE=1)"; \
		exit 1; \
	fi
	-$(UV) tool uninstall $(NAME)
	@if [ "$$(readlink -f $(WRAPPER_LINK) 2>/dev/null)" = "$$(readlink -f $(WRAPPER_SRC))" ]; then \
		rm -f $(WRAPPER_LINK); echo "removed $(WRAPPER_LINK)"; \
	elif [ -e $(WRAPPER_LINK) ] || [ -L $(WRAPPER_LINK) ]; then \
		echo "kept $(WRAPPER_LINK): it does not point at this repo"; \
	fi
	@echo
	@echo "note: open shells keep the function until they restart (unset -f $(NAME) clears it)"
	@echo "      /tmp/$(NAME)-$$(id -u) (sockets, metadata, raw logs) is left alone"

test:
	$(UV) run python -m unittest discover -s tests -v
