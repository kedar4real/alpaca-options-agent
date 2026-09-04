import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import LandingPage from './LandingPage'

/* The hackathon landing page is the home route and the deployed entry point.
   The original live React dashboard (which needs the local /api server and
   pulls in recharts) stays reachable at #/legacy so nothing is lost, but it
   is lazy-loaded so it never weighs down the landing bundle. The read-only
   audit dashboard ships separately as a Streamlit app. */
const App = lazy(() => import('./App'))

const isLegacy =
  window.location.hash.replace(/^#/, '').replace(/\/$/, '') === '/legacy'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {isLegacy ? (
      <Suspense fallback={null}>
        <App />
      </Suspense>
    ) : (
      <LandingPage />
    )}
  </StrictMode>,
)
