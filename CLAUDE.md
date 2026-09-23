# Claude Instructions for Label Sheet Generator

## Project Overview

This is a Python-based label sheet generator that creates PDF documents from templates and data. The system supports various label formats, including Avery templates, and provides both command-line and web interfaces.

## Key Components

1. **Models**: Data structures defining templates, page specifications, and elements
2. **Rendering**: PDF generation logic using ReportLab
3. **Workspace**: Configuration management for the generation process
4. **Presets**: Predefined templates (especially Avery labels)
5. **Interfaces**: Command-line and web interfaces

## Core Functionality

### Data Models
- `LabelTemplate`: Defines label dimensions and grid layout
- `PageSpec`: Controls page orientation and margins
- `WorkspaceConfig`: Combines template and page settings
- `LabelElement`: Individual elements (text, barcode, image) within labels

### PDF Generation Process
1. Create canvas with specified page size
2. Apply margins to calculate usable space
3. Calculate label positions based on grid configuration
4. Render each label with content from data
5. Save as PDF file

### Template System
- Predefined Avery templates (5160, 5161, 5162, etc.)
- Configurable grid layouts (rows/columns)
- Support for custom templates

## Key Features
- Multiple template formats support
- Configurable page margins and orientation
- Data-driven label content generation
- Web interface via Streamlit
- REST API endpoints
- PDF output with ReportLab

## Usage Patterns

### Command Line
```bash
python -m label_sheet_generator
```

### Web Interface
```bash
python -m label_sheet_generator web
```

### Programmatic Usage
```python
from label_sheet_generator import generate_label_sheet
from label_sheet_generator.models import LabelTemplate, PageSpec

# Create template
template = LabelTemplate("Avery 5160", 66.67, 25.4, GridSpec(3, 10))

# Define page settings
page_spec = PageSpec("portrait", 0, 0, 0, 0)

# Generate PDF
generate_label_sheet(template, page_spec, data, "output.pdf")
```

## Code Structure Guidelines

1. **Modular Design**: Each component has a single responsibility
2. **Type Hints**: All functions and methods should include type hints
3. **Documentation**: Every public function/class should have docstrings
4. **Error Handling**: Proper exception handling with meaningful messages
5. **Testing**: Unit tests for core functionality

## Development Notes

- Use ReportLab for PDF generation
- Support both portrait and landscape orientations
- Handle margins properly in calculations
- Make templates easily extensible
- Ensure proper unit conversions (mm to points)
- Maintain backward compatibility when possible