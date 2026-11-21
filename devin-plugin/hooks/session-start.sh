#!/bin/sh

prepare_parent() (
    parent=$(dirname "$1")
    component=$parent
    while [ "$component" != . ]; do
        [ ! -L "$component" ] || exit 1
        component=$(dirname "$component")
    done
    mkdir -p "$parent" || exit 1
    physical_parent=$(cd -P "$parent" && pwd -P) || exit 1
    case "$physical_parent/" in
        "$physical_root/"*) exit 0 ;;
        *) exit 1 ;;
    esac
)

exclude_created() {
    file=$1
    git check-ignore -q -- "$file" && return
    [ -z "$(git ls-files -- "$file")" ] || return
    exclude=$(git rev-parse --git-path info/exclude) || return
    mkdir -p "$(dirname "$exclude")" || return
    printf '\n/%s\n' "$file" >> "$exclude"
}

prepare_repo() (
    cd "$1" || exit 0
    [ -e .git ] || exit 0
    root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
    cd "$root" || exit 0
    physical_root=$(pwd -P) || exit 0
    config=.punt-labs/biff/config.local.yaml
    identity=.punt-labs/ethos/identities/devin.yaml
    if [ ! -e "$config" ] && [ ! -L "$config" ] && prepare_parent "$config"; then
        if (set -C; printf '%s\n' \
            'team:' \
            '  members: [jmf-pobox, github-actions]' \
            'peers:' \
            '  orgs: [punt-labs]' \
            '  repos: [local-scripts]' > "$config"); then
            exclude_created "$config"
        fi
    fi
    if [ ! -e "$identity" ] && [ ! -L "$identity" ] && prepare_parent "$identity"; then
        if (set -C; printf '%s\n' \
            'name: Devin' \
            'handle: devin' \
            'kind: agent' \
            'email: "158243242+devin-ai-integration[bot]@users.noreply.github.com"' \
            'github: "devin-ai-integration[bot]"' > "$identity"); then
            exclude_created "$identity"
        fi
    fi
)

seen='
'
for repo in "${DEVIN_PROJECT_DIR:-}" "$HOME"/repos/*; do
    [ -n "$repo" ] && [ -e "$repo/.git" ] || continue
    root=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null) || continue
    case "$seen" in
        *"
$root
"*) continue ;;
    esac
    seen="$seen$root
"
    prepare_repo "$root" >/dev/null 2>&1
done
exit 0
