#!/bin/bash
# Ask the user for input to customize the template
read -p "Enter the project name (e.g., my-service): " PROJECT_NAME

# Replace placeholders in targeted files only
FILES=(
    "docker-compose.yaml"
    "Makefile"
    "pyproject.toml"
    "README.md"
    "CLAUDE.md"
    "AGENTS.md"
    "uv.lock"
    "app/blueprint.py"
    "app/config.py"
    "app/docs.py"
    "app/main.py"
    "app/models.py"
    "docs/DEVELOPMENT.md"
    "tests/test_main.py"
)

for FILE in "${FILES[@]}"; do
    if [ -f "$FILE" ]; then
        sed -i "s/backend-template/$PROJECT_NAME/g" "$FILE"
    fi
done

# if .env does not exist, create from the example
if [ ! -f ".env" ]; then
    cp .env.example .env
fi

# Replace - with _ in the project name for python module naming
PROJECT_NAME_UNDERSCORED=$(echo "$PROJECT_NAME" | tr '-' '_')
mv app/backend-template app/$PROJECT_NAME_UNDERSCORED

# Clean up: remove this script, its make target, and its docs entries.
rm -f scripts/init_template.sh

sed -i '/make init.*Initialize project from template/d' Makefile
sed -i '/^init:$/d' Makefile
sed -i '/^\tbash scripts\/init_template\.sh$/d' Makefile
sed -i 's/ init//' Makefile
sed -i '/^$/N;/^\n$/d' Makefile
sed -i '24s/^/\n/' Makefile

sed -i '/init_template\.sh.*Rename template to your project/d' docs/DEVELOPMENT.md
# remove lines 2-5 in README.md
sed -i '2,5d' README.md
