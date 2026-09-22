import { describe, expect, it } from 'vitest'
import { File, FileKey, FileSpreadsheet, FileText } from 'lucide-react'

import { fileFamilyOf, fileTypeIcon, fileTypeLabel } from './fileTypeIcon'

describe('fileFamilyOf', () => {
  it('reads the MIME type first', () => {
    expect(fileFamilyOf('report.bin', 'application/pdf')).toBe('document')
    expect(fileFamilyOf('x', 'image/heic')).toBe('image')
    expect(fileFamilyOf('x', 'video/quicktime')).toBe('video')
    expect(fileFamilyOf('x', 'audio/flac')).toBe('audio')
    expect(fileFamilyOf('x', 'font/woff2')).toBe('font')
    expect(fileFamilyOf('x', 'model/gltf-binary')).toBe('model3d')
  })

  it('ignores MIME parameters', () => {
    expect(fileFamilyOf('notes.txt', 'text/plain; charset=utf-8')).toBe('document')
  })

  it('falls back to the extension for octet-stream and missing types', () => {
    expect(fileFamilyOf('data.xlsx', 'application/octet-stream')).toBe('spreadsheet')
    expect(fileFamilyOf('deck.pptx')).toBe('slides')
    expect(fileFamilyOf('deploy-key.pem')).toBe('key')
    expect(fileFamilyOf('site.tar.gz')).toBe('archive')
    expect(fileFamilyOf('app.dmg')).toBe('installer')
    expect(fileFamilyOf('analysis.ipynb')).toBe('notebook')
    expect(fileFamilyOf('part.stl')).toBe('model3d')
    expect(fileFamilyOf('book.epub')).toBe('ebook')
    expect(fileFamilyOf('invite.ics')).toBe('calendar')
    expect(fileFamilyOf('server.log')).toBe('log')
    expect(fileFamilyOf('fix.patch')).toBe('patch')
    expect(fileFamilyOf('cache.sqlite')).toBe('database')
  })

  it('is case-insensitive and reads only the basename', () => {
    expect(fileFamilyOf('OUT/REPORT.PDF')).toBe('document')
    expect(fileFamilyOf('dir.with.dots/readme')).toBe('unknown')
  })

  it('treats a dotfile name as its extension', () => {
    expect(fileFamilyOf('.env')).toBe('config')
    expect(fileFamilyOf('.gitignore')).toBe('unknown')
  })

  it('never guesses: unknown types and no extension are `unknown`', () => {
    expect(fileFamilyOf('blob')).toBe('unknown')
    expect(fileFamilyOf('thing.xyz123', 'application/x-made-up')).toBe('unknown')
  })
})

describe('fileTypeIcon', () => {
  it('maps a family to one lucide icon and a theme tone class', () => {
    expect(fileTypeIcon('a.pdf')).toMatchObject({ family: 'document', Icon: FileText, tone: 'text-info' })
    expect(fileTypeIcon('a.csv')).toMatchObject({ family: 'spreadsheet', Icon: FileSpreadsheet })
    expect(fileTypeIcon('a.crt')).toMatchObject({ family: 'key', Icon: FileKey, tone: 'text-danger' })
    expect(fileTypeIcon('blob')).toMatchObject({ family: 'unknown', Icon: File })
  })

  it('only ever emits theme text-colour utilities as tones', () => {
    const exts = ['pdf', 'csv', 'pptx', 'png', 'mp4', 'mp3', 'ts', 'json', 'log', 'zip', 'pem', 'dmg', 'db', 'patch', 'ttf', 'ini', 'ipynb', 'stl', 'epub', 'ics', 'xyz']
    for (const e of exts) expect(fileTypeIcon(`f.${e}`).tone).toMatch(/^text-(text|muted|accent|ok|warn|danger|info)$/)
  })
})

describe('fileTypeLabel', () => {
  it('upper-cases the extension and is empty without one', () => {
    expect(fileTypeLabel('q3-report.pdf')).toBe('PDF')
    expect(fileTypeLabel('site.tar.gz')).toBe('GZ')
    expect(fileTypeLabel('blob')).toBe('')
    expect(fileTypeLabel('.env')).toBe('ENV')
  })
})
