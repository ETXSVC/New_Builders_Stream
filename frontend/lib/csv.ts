// Hand-rolled parser/serializer for the catalog import/export format:
// four columns, header row required — category,name,unit,unit_rate.
// No library: this format has no embedded commas beyond what basic quote
// handling below covers, and pulling in a CSV dependency for four columns
// is unwarranted (spec Decision 9).

export interface CatalogCsvRow {
  category: string;
  name: string;
  unit: string;
  unit_rate: string;
}

const EXPECTED_HEADER = ["category", "name", "unit", "unit_rate"];

export class CsvParseError extends Error {}

function parseLine(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (inQuotes) {
      if (char === '"' && line[i + 1] === '"') {
        current += '"';
        i++;
      } else if (char === '"') {
        inQuotes = false;
      } else {
        current += char;
      }
    } else if (char === '"') {
      inQuotes = true;
    } else if (char === ",") {
      fields.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  fields.push(current);
  return fields;
}

export function parseCatalogCsv(text: string): CatalogCsvRow[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n").filter((l) => l.trim() !== "");
  if (lines.length === 0) throw new CsvParseError("File is empty");

  const header = parseLine(lines[0]).map((h) => h.trim().toLowerCase());
  if (header.length !== 4 || EXPECTED_HEADER.some((col, i) => header[i] !== col)) {
    throw new CsvParseError(`Header must be exactly: ${EXPECTED_HEADER.join(",")}`);
  }

  return lines.slice(1).map((line, index) => {
    const fields = parseLine(line);
    if (fields.length !== 4) {
      throw new CsvParseError(`Row ${index + 2} has ${fields.length} columns, expected 4`);
    }
    const [category, name, unit, unit_rate] = fields;
    return { category: category.trim(), name: name.trim(), unit: unit.trim(), unit_rate: unit_rate.trim() };
  });
}

function escapeCsvField(value: string): string {
  if (value.includes(",") || value.includes('"') || value.includes("\n")) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

export function serializeCatalogCsv(rows: CatalogCsvRow[]): string {
  const lines = [EXPECTED_HEADER.join(",")];
  for (const row of rows) {
    lines.push(
      [row.category, row.name, row.unit, row.unit_rate].map(escapeCsvField).join(",")
    );
  }
  return lines.join("\n");
}
