import { useState, useCallback } from 'react'

/**
 * Reusable data-fetching hook.
 * Eliminates the repeated loading/error state pattern across 13+ pages.
 *
 * Usage:
 *   const { data, loading, error, execute } = useFetch()
 *
 *   // In useEffect or event handler:
 *   execute(() => someAPI.list(params))
 *
 *   // Or with auto-set:
 *   execute(async () => {
 *     const res = await api.get('/something')
 *     return res.data
 *   })
 */
export default function useFetch(initialData = null) {
  const [data, setData] = useState(initialData)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const execute = useCallback(async (asyncFn) => {
    setLoading(true)
    setError(null)
    try {
      const result = await asyncFn()
      setData(result)
      return result
    } catch (e) {
      setError(e)
      throw e
    } finally {
      setLoading(false)
    }
  }, [])

  const reset = useCallback(() => {
    setData(initialData)
    setError(null)
    setLoading(false)
  }, [initialData])

  return { data, setData, loading, error, execute, reset }
}
