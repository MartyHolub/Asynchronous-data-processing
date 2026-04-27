import { useState, useEffect, useCallback } from 'react'

const API = '/api'

const OPERATIONS = [
  { key: 'grayscale', label: 'Grayscale', params: {} },
  { key: 'invert', label: 'Invert', params: {} },
  { key: 'flip_horizontal', label: 'Flip H', params: {} },
  { key: 'brightness', label: 'Brighten +50', params: { value: 50 } },
  { key: 'crop', label: 'Crop 20px', params: { top: 20, bottom: 20, left: 20, right: 20 } },
]

export default function App() {
  const [bucketId, setBucketId] = useState('')
  const [activeBucket, setActiveBucket] = useState(null)
  const [objects, setObjects] = useState([])
  const [jobs, setJobs] = useState([])
  const [error, setError] = useState(null)
  const [uploading, setUploading] = useState(false)

  const fetchObjects = useCallback(async (bid) => {
    if (!bid) return
    try {
      const res = await fetch(`${API}/buckets/${bid}/objects`)
      if (!res.ok) throw new Error(await res.text())
      setObjects(await res.json())
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => {
    if (activeBucket) fetchObjects(activeBucket)
  }, [activeBucket, fetchObjects])

  const createBucket = async () => {
    setError(null)
    try {
      const res = await fetch(`${API}/buckets`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bucket_id: bucketId }),
      })
      if (!res.ok) throw new Error(await res.text())
      setActiveBucket(bucketId)
    } catch (e) { setError(e.message) }
  }

  const uploadFile = async (e) => {
    const file = e.target.files[0]
    if (!file || !activeBucket) return
    setUploading(true)
    const form = new FormData()
    form.append('file', file)
    try {
      const res = await fetch(`${API}/buckets/${activeBucket}/objects`, { method: 'POST', body: form })
      if (!res.ok) throw new Error(await res.text())
      fetchObjects(activeBucket)
    } catch (e) { setError(e.message) }
    setUploading(false)
  }

  const processImage = async (objectId, operation, params) => {
    setError(null)
    try {
      const res = await fetch(`${API}/buckets/${activeBucket}/objects/${objectId}/process`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operation, params }),
      })
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      setJobs(prev => [{ ...data, objectId, operation, ts: new Date().toLocaleTimeString() }, ...prev.slice(0, 19)])
      setTimeout(() => fetchObjects(activeBucket), 3000)
    } catch (e) { setError(e.message) }
  }

  return (
    <div className="app">
      <h1>🖼️ Async Image Processing</h1>

      <section className="card">
        <h2>Bucket</h2>
        <div className="row">
          <input value={bucketId} onChange={e => setBucketId(e.target.value)} placeholder="bucket-name" />
          <button onClick={createBucket}>Create / Select</button>
        </div>
        {activeBucket && <p className="info">Active bucket: <strong>{activeBucket}</strong></p>}
      </section>

      {activeBucket && (
        <section className="card">
          <h2>Upload Image</h2>
          <input type="file" accept="image/*" onChange={uploadFile} disabled={uploading} />
          {uploading && <span>Uploading…</span>}
        </section>
      )}

      {objects.length > 0 && (
        <section className="card">
          <h2>Objects in <em>{activeBucket}</em></h2>
          <div className="grid">
            {objects.map(obj => (
              <div key={obj.object_id} className="obj-card">
                <img src={`${API}/buckets/${activeBucket}/objects/${obj.object_id}`} alt={obj.object_id} />
                <p className="obj-name">{obj.object_id}</p>
                <div className="ops">
                  {OPERATIONS.map(op => (
                    <button key={op.key} onClick={() => processImage(obj.object_id, op.key, op.params)}>
                      {op.label}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
          <button className="refresh" onClick={() => fetchObjects(activeBucket)}>🔄 Refresh</button>
        </section>
      )}

      {jobs.length > 0 && (
        <section className="card">
          <h2>Processing Jobs</h2>
          <table>
            <thead><tr><th>Time</th><th>Object</th><th>Operation</th><th>Status</th><th>Job ID</th></tr></thead>
            <tbody>
              {jobs.map((j, i) => (
                <tr key={i}>
                  <td>{j.ts}</td>
                  <td>{j.objectId}</td>
                  <td>{j.operation}</td>
                  <td className="status-processing">{j.status}</td>
                  <td className="job-id">{j.job_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {error && <div className="error">⚠️ {error}</div>}
    </div>
  )
}
