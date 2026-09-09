import { useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useBetStore } from '../store/useBetStore';
import type { BoardMarket, LedgerPlacement, ModelName, ScoutMessage, WalletSnapshot } from '../store/useBetStore';

const base = '/api/v1/board';
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function finite(value: unknown