import { useEffect, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { fetchHealth } from '@/lib/api'

type HealthState = 'checking' | 'healthy' | 'unhealthy'

const LABELS: Record<HealthState, string> = {
  checking: 'Checking API…',
  healthy: 'API healthy',
  unhealthy: 'API unreachable',
}

function App() {
  const [state, setState] = useState<HealthState>('checking')

  useEffect(() => {
    const controller = new AbortController()
    fetchHealth(controller.signal)
      .then((ok) => setState(ok ? 'healthy' : 'unhealthy'))
      .catch(() => {
        if (!controller.signal.aborted) setState('unhealthy')
      })
    return () => controller.abort()
  }, [])

  return (
    <main className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>DRHP Red-Flag Agent</CardTitle>
          <CardDescription>Backend status</CardDescription>
        </CardHeader>
        <CardContent>
          <Badge
            data-testid="health-status"
            variant={state === 'unhealthy' ? 'destructive' : state === 'healthy' ? 'default' : 'secondary'}
          >
            {LABELS[state]}
          </Badge>
        </CardContent>
      </Card>
    </main>
  )
}

export default App
