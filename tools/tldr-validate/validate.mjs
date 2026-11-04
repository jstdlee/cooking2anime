// Validate .tldr files against the pinned @tldraw/tlschema.
// Usage: node validate.mjs board.tldr [...]          -> exit 1 on any error
//        node validate.mjs --print-schema            -> serialized schema JSON
import { readFileSync } from 'node:fs'
import { createTLSchema } from '@tldraw/tlschema'

const schema = createTLSchema()
const args = process.argv.slice(2)

if (args[0] === '--print-schema') {
  process.stdout.write(JSON.stringify(schema.serialize(), null, 2) + '\n')
  process.exit(0)
}

let failed = 0
for (const file of args) {
  const errors = []
  try {
    const doc = JSON.parse(readFileSync(file, 'utf8'))
    if (doc.tldrawFileFormatVersion !== 1) errors.push('tldrawFileFormatVersion must be 1')
    const store = Object.fromEntries(doc.records.map((r) => [r.id, r]))
    const migrated = schema.migrateStoreSnapshot({ store, schema: doc.schema })
    if (migrated.type !== 'success') {
      errors.push(`migration failed: ${migrated.reason}`)
    } else {
      for (const rec of Object.values(migrated.value)) {
        const type = schema.types[rec.typeName]
        if (!type) { errors.push(`${rec.id}: unknown typeName ${rec.typeName}`); continue }
        try { type.validate(rec) } catch (e) { errors.push(`${rec.id}: ${e.message}`) }
      }
      const ids = new Set(Object.keys(migrated.value))
      for (const rec of Object.values(migrated.value)) {
        if (rec.typeName === 'shape' && !ids.has(rec.parentId)) errors.push(`${rec.id}: missing parent ${rec.parentId}`)
        if (rec.typeName === 'shape' && rec.props?.assetId && !ids.has(rec.props.assetId)) errors.push(`${rec.id}: missing asset ${rec.props.assetId}`)
      }
    }
  } catch (e) {
    errors.push(e.message)
  }
  if (errors.length) {
    failed++
    console.error(`FAIL ${file}`)
    for (const e of errors) console.error(`  ${e}`)
  } else {
    console.log(`ok   ${file}`)
  }
}
process.exit(failed ? 1 : 0)
