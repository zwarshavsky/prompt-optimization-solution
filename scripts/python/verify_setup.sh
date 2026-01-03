#!/bin/bash
# Verify that the virtual environment is set up correctly

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

echo "Verifying Python environment setup..."
echo ""

# Check if venv exists
if [ ! -d "$VENV_DIR" ]; then
    echo "❌ Virtual environment not found. Run ./setup_env.sh first."
    exit 1
fi

# Activate and check
source "$VENV_DIR/bin/activate"

echo "✅ Virtual environment: $VENV_DIR"
echo "✅ Python: $(which python)"
echo "✅ Python version: $(python --version)"

# Check required packages
echo ""
echo "Checking required packages..."

python -c "import requests; print('✅ requests:', requests.__version__)" 2>/dev/null || echo "❌ requests not installed"
python -c "import urllib3; print('✅ urllib3:', urllib3.__version__)" 2>/dev/null || echo "❌ urllib3 not installed"

# Check if main module can be imported
echo ""
echo "Checking module imports..."
python -c "from search_index_api import SearchIndexAPI; print('✅ SearchIndexAPI imported successfully')" 2>/dev/null || echo "❌ Failed to import SearchIndexAPI"

echo ""
echo "✅ Setup verification complete!"



