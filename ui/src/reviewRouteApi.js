// The initial review gate needs two pieces of the API security boundary before any run workspace is
// loaded. Keep that deliberately narrow reachability edge explicit: importing the api.js barrel here
// made every command, storage, SSE and paid-work protocol eager on the empty/login shell.
export { get, reviewTokenFromLocation } from './apiClient.js'
