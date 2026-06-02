import { useCallback, useEffect, useState } from "react"

import { ApiError } from "@/api/client"

/** Minimal load/refresh/error helper for the admin tabs. `loader` is called
 * on mount and whenever `reload()` is invoked (e.g. after a mutation). */
export function useAsync<T>(loader: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await loader())
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.status === 403
            ? "Forbidden — admin role required."
            : `${e.message}${e.body ? ": " + JSON.stringify(e.body) : ""}`
          : String(e)
      setError(msg)
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  return { data, error, loading, reload, setError }
}
