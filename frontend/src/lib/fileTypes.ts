export interface FileTypeMeta {
  label: string;
  badge: string;
  bg: string;
  border: string;
  text: string;
  use: string;
}

// All file-type badges use a single black-and-white treatment.
// White background · black text · standard border.
const BW: Pick<FileTypeMeta, 'bg' | 'border' | 'text'> = {
  bg: '#ffffff',
  border: '#111111',
  text: '#111111',
};

export const FILE_TYPE_META: Record<string, FileTypeMeta> = {
  pdf:  { label: 'PDF',        badge: 'PDF',  ...BW, use: 'Research · POVs · Proposals' },
  docx: { label: 'Word',       badge: 'DOCX', ...BW, use: 'BRDs · Requirements · Specs' },
  pptx: { label: 'PowerPoint', badge: 'PPTX', ...BW, use: 'Decks · Architecture · POVs' },
  xlsx: { label: 'Excel',      badge: 'XLSX', ...BW, use: 'Requirements matrices · Data' },
  xls:  { label: 'Excel (97)', badge: 'XLS',  ...BW, use: 'Legacy spreadsheets' },
  csv:  { label: 'CSV',        badge: 'CSV',  ...BW, use: 'Structured data exports' },
};

export function getFileTypeMeta(fileType: string): FileTypeMeta {
  return FILE_TYPE_META[fileType?.toLowerCase()] ?? {
    label: fileType?.toUpperCase() ?? 'Doc',
    badge: fileType?.toUpperCase() ?? 'Doc',
    ...BW,
    use: '',
  };
}

export function fileTypeLabel(fileType: string): string {
  return FILE_TYPE_META[fileType?.toLowerCase()]?.label ?? fileType?.toUpperCase() ?? 'Doc';
}
