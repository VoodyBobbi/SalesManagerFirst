"""SALES MANAGER FIRST desktop entry point."""
import sys

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-test':
        from sales_manager.core.smoke import smoke
        smoke(sys.argv[2])
    else:
        from sales_manager.ui.app import run
        run()
