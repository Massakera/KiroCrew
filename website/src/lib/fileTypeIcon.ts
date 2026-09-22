/**
 * File-type → icon mapping for the attachment card.
 *
 * Files come in hundreds of formats; the card shows ONE of a small set of
 * families so a reader can tell "a spreadsheet" from "a certificate" at a
 * glance without reading the extension. MIME wins when the sender supplied
 * one (it is what the server actually knows about the bytes); the extension
 * is the fallback for `application/octet-stream` and for senders that set no
 * type at all. Anything unrecognised is the plain `File` glyph — never a
 * wrong family, because a wrong glyph is worse than a generic one.
 *
 * Icons are lucide only (AUTOSDE `use-lucide-icons`), and the tone is a theme
 * text-colour utility so it follows the active theme.
 */
import type { LucideIcon } from 'lucide-react'
import {
  BookOpen,
  Box,
  CalendarDays,
  Database,
  File,
  FileArchive,
  FileAudio,
  FileCode,
  FileCog,
  FileDiff,
  FileImage,
  FileJson,
  FileKey,
  FileSpreadsheet,
  FileTerminal,
  FileText,
  FileType,
  FileVideo,
  NotebookText,
  Package,
  Presentation,
} from 'lucide-react'

export type FileFamily =
  | 'document'
  | 'spreadsheet'
  | 'slides'
  | 'image'
  | 'video'
  | 'audio'
  | 'code'
  | 'data'
  | 'log'
  | 'archive'
  | 'key'
  | 'installer'
  | 'database'
  | 'patch'
  | 'font'
  | 'config'
  | 'notebook'
  | 'model3d'
  | 'ebook'
  | 'calendar'
  | 'unknown'

export interface FileTypeIcon {
  family: FileFamily
  Icon: LucideIcon
  /** Theme text-colour utility for the glyph. */
  tone: string
}

const FAMILY_ICON: Record<FileFamily, FileTypeIcon> = {
  document: { family: 'document', Icon: FileText, tone: 'text-info' },
  spreadsheet: { family: 'spreadsheet', Icon: FileSpreadsheet, tone: 'text-ok' },
  slides: { family: 'slides', Icon: Presentation, tone: 'text-warn' },
  image: { family: 'image', Icon: FileImage, tone: 'text-accent' },
  video: { family: 'video', Icon: FileVideo, tone: 'text-accent' },
  audio: { family: 'audio', Icon: FileAudio, tone: 'text-accent' },
  code: { family: 'code', Icon: FileCode, tone: 'text-text' },
  data: { family: 'data', Icon: FileJson, tone: 'text-text' },
  log: { family: 'log', Icon: FileTerminal, tone: 'text-muted' },
  archive: { family: 'archive', Icon: FileArchive, tone: 'text-muted' },
  key: { family: 'key', Icon: FileKey, tone: 'text-danger' },
  installer: { family: 'installer', Icon: Package, tone: 'text-muted' },
  database: { family: 'database', Icon: Database, tone: 'text-muted' },
  patch: { family: 'patch', Icon: FileDiff, tone: 'text-ok' },
  font: { family: 'font', Icon: FileType, tone: 'text-muted' },
  config: { family: 'config', Icon: FileCog, tone: 'text-muted' },
  notebook: { family: 'notebook', Icon: NotebookText, tone: 'text-warn' },
  model3d: { family: 'model3d', Icon: Box, tone: 'text-accent' },
  ebook: { family: 'ebook', Icon: BookOpen, tone: 'text-info' },
  calendar: { family: 'calendar', Icon: CalendarDays, tone: 'text-info' },
  unknown: { family: 'unknown', Icon: File, tone: 'text-muted' },
}

/** Exact MIME types that a `startsWith` prefix rule would misfile. */
const MIME_EXACT: Record<string, FileFamily> = {
  'application/pdf': 'document',
  'application/msword': 'document',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'document',
  'application/vnd.oasis.opendocument.text': 'document',
  'application/rtf': 'document',
  'text/plain': 'document',
  'text/markdown': 'document',
  'application/vnd.ms-excel': 'spreadsheet',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'spreadsheet',
  'application/vnd.oasis.opendocument.spreadsheet': 'spreadsheet',
  'text/csv': 'spreadsheet',
  'text/tab-separated-values': 'spreadsheet',
  'application/vnd.ms-powerpoint': 'slides',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'slides',
  'application/vnd.oasis.opendocument.presentation': 'slides',
  'application/json': 'data',
  'application/xml': 'data',
  'text/xml': 'data',
  'application/yaml': 'data',
  'application/toml': 'data',
  'text/html': 'code',
  'text/css': 'code',
  'text/javascript': 'code',
  'application/javascript': 'code',
  'application/typescript': 'code',
  'application/zip': 'archive',
  'application/gzip': 'archive',
  'application/x-tar': 'archive',
  'application/x-7z-compressed': 'archive',
  'application/vnd.rar': 'archive',
  'application/x-bzip2': 'archive',
  'application/x-xz': 'archive',
  'application/x-pem-file': 'key',
  'application/x-x509-ca-cert': 'key',
  'application/pkcs12': 'key',
  'application/x-pkcs12': 'key',
  'application/vnd.apple.diskimage': 'installer',
  'application/x-apple-diskimage': 'installer',
  'application/x-msdownload': 'installer',
  'application/x-msi': 'installer',
  'application/vnd.debian.binary-package': 'installer',
  'application/x-rpm': 'installer',
  'application/vnd.android.package-archive': 'installer',
  'application/vnd.sqlite3': 'database',
  'application/x-sqlite3': 'database',
  'application/sql': 'database',
  'application/vnd.apache.parquet': 'database',
  'text/x-diff': 'patch',
  'text/x-patch': 'patch',
  'application/epub+zip': 'ebook',
  'application/x-mobipocket-ebook': 'ebook',
  'text/calendar': 'calendar',
  'text/vcard': 'calendar',
  'application/x-ipynb+json': 'notebook',
  'model/stl': 'model3d',
  'model/obj': 'model3d',
  'model/gltf+json': 'model3d',
  'model/gltf-binary': 'model3d',
}

/** Lower-case extension (no dot) → family. */
const EXT: Record<string, FileFamily> = Object.fromEntries(
  (
    [
      ['document', 'pdf doc docx odt rtf txt md markdown pages'],
      ['spreadsheet', 'xls xlsx xlsm csv tsv ods numbers'],
      ['slides', 'ppt pptx key odp'],
      ['image', 'png jpg jpeg gif webp svg heic heif bmp tiff tif avif ico'],
      ['video', 'mp4 mov webm mkv avi m4v'],
      ['audio', 'mp3 wav m4a flac ogg aac opus'],
      ['code', 'ts tsx js jsx mjs cjs py go rs java kt swift c h cpp hpp cs rb php sh bash zsh ps1 html css scss less vue svelte'],
      ['data', 'json yaml yml toml xml jsonl ndjson'],
      ['log', 'log out err trace'],
      ['archive', 'zip tar gz tgz bz2 xz 7z rar zst'],
      ['key', 'pem key crt cer der p12 pfx pub asc gpg'],
      ['installer', 'dmg pkg exe msi deb rpm apk appimage whl'],
      ['database', 'sqlite sqlite3 db sql parquet'],
      ['patch', 'patch diff'],
      ['font', 'ttf otf woff woff2'],
      ['config', 'env ini conf cfg properties'],
      ['notebook', 'ipynb rmd'],
      ['model3d', 'stl obj glb gltf fbx dwg dxf'],
      ['ebook', 'epub mobi azw3'],
      ['calendar', 'ics vcf'],
    ] as [FileFamily, string][]
  ).flatMap(([family, exts]) => exts.split(' ').map(e => [e, family] as const)),
)

/** `.env`, `.gitignore`: a leading dot with no other dot is the whole name, not an extension. */
function extensionOf(filename: string): string {
  const base = filename.split('/').pop() ?? filename
  const dot = base.lastIndexOf('.')
  if (dot <= 0) return base.startsWith('.') ? base.slice(1).toLowerCase() : ''
  return base.slice(dot + 1).toLowerCase()
}

export function fileFamilyOf(filename: string, contentType?: string | null): FileFamily {
  const mime = (contentType || '').split(';')[0].trim().toLowerCase()
  if (mime) {
    const exact = MIME_EXACT[mime]
    if (exact) return exact
    if (mime.startsWith('image/')) return 'image'
    if (mime.startsWith('video/')) return 'video'
    if (mime.startsWith('audio/')) return 'audio'
    if (mime.startsWith('font/')) return 'font'
    if (mime.startsWith('model/')) return 'model3d'
  }
  return EXT[extensionOf(filename)] ?? 'unknown'
}

export function fileTypeIcon(filename: string, contentType?: string | null): FileTypeIcon {
  return FAMILY_ICON[fileFamilyOf(filename, contentType)]
}

/** Short type label for the card's meta line: the extension, upper-cased
 *  (`PDF`, `XLSX`). Derived from the name, so it needs no catalog entry and
 *  reads the same in every locale. Empty when the name has no extension. */
export function fileTypeLabel(filename: string): string {
  return extensionOf(filename).toUpperCase()
}
