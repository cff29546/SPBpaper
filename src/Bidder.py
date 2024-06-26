import matplotlib.pyplot as plt
import numpy as np
import scipy.stats
import torch
from scipy import optimize

from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF
from sklearn.metrics import roc_auc_score
from tqdm import tqdm

from Impression import ImpressionOpportunity
from Models import BidShadingContextualBandit, BidShadingPolicy, PyTorchWinRateEstimator


class Bidder:
    """ Bidder base class"""
    def __init__(self, rng):
        self.rng = rng
        self.truthful = False # Default

    def update(self, contexts, values, bids, prices, outcomes, estimated_CTRs, won_mask, iteration, plot, figsize, fontsize, name):
        pass

    def clear_logs(self, memory):
        pass


class TruthfulBidder(Bidder):
    """ A bidder that bids truthfully """
    def __init__(self, rng):
        super(TruthfulBidder, self).__init__(rng)
        self.truthful = True

    def bid(self, value, context, estimated_CTR):
        return value * estimated_CTR


class BudgetRistrictedBidder(Bidder):
    """ A bidder with budget ristriction """
    def __init__(self, rng, budget_per_iter_range, rounds_per_iter):
        super(BudgetRistrictedBidder, self).__init__(rng)
        budget = rng.uniform(*budget_per_iter_range)
        self.budget = budget
        self.spending = 0

    def charge(self, price, round, estimated_CTR, value):
        self.spending += price

    def update(self, contexts, values, bids, prices, outcomes, estimated_CTRs, won_mask, iteration, plot, figsize, fontsize, name):
        self.spending = 0


class TruthfulBudgetRistricctedBidder(BudgetRistrictedBidder):
    """ A simple bidder with budget ristriction """
    def __init__(self, rng, budget_per_iter_range, rounds_per_iter):
        super(TruthfulBudgetRistricctedBidder, self).__init__(rng, budget_per_iter_range, rounds_per_iter)
        self.truthful = True

    def bid(self, value, context, estimated_CTR):
        if self.spending < self.budget:
            return value * estimated_CTR
        return 0


def aggregate_near_sample(samples, distance=1e-6):
    result = []
    current = []
    for s in samples:
        if len(current) == 0 or s[0] < current[0][0] * (1.0 + distance):
            current.append(s)
        else:
            result.append(list(map(np.mean, zip(*current))))
            current = []

    if len(current) > 0:
        result.append(list(map(np.mean, zip(*current))))

    return result


def increasing_subsequence(samples):
    result = []
    for s in samples:
        l = 0
        r = len(result)
        while l < r:
            mid = (l + r) // 2
            if s[1] >= result[mid][1]:
                l = mid + 1
            else:
                r = mid
        if l == len(result):
            result.append(s)
        else:
            result[l] = s
    return result


def liner_solve(x1, y1, x2, y2, x):
    if np.abs(x1 - x2) < 1e-6:
        return (y1 + y2) / 2
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1)


def impc(bid2spend, target):
    samples = sorted(bid2spend)
    samples = aggregate_near_sample(samples)
    samples = increasing_subsequence(samples)
    if len(samples) == 0:
        return 1.0
    bid = 0
    spend = 0
    i = 0
    while i < len(samples) and samples[i][1] < target:
        bid = samples[i][0]
        spend = samples[i][1]
        i += 1
    if i < len(samples):
        return liner_solve(spend, bid, samples[i][1], samples[i][0], target)
    else:
        return liner_solve(0, 0, samples[-1][1], samples[-1][0], target)


class IMPCBudgetBidder(BudgetRistrictedBidder):
    """ IMPC Budget pacing bidder """
    def __init__(self, rng, budget_per_iter_range, rounds_per_iter, rounds_per_step, bid_step, memory):
        super(IMPCBudgetBidder, self).__init__(rng, budget_per_iter_range, rounds_per_iter)
        self.rounds_per_step = rounds_per_step
        self.rounds_per_iter = rounds_per_iter
        self.target_step_spending = self.budget * rounds_per_step / rounds_per_iter
        self.bid2spend_history = []
        self.bid_step = bid_step
        self.roi_bid = 1.0
        self.step_spending = 0
        self.memory = memory

    def calc_roi_bid(self, target_step_spending):
        if self.step_spending < 1e-6:
            return self.roi_bid + self.bid_step
        else:
            bid = impc(self.bid2spend_history, target_step_spending)
            return np.minimum(np.maximum(bid, self.roi_bid - self.bid_step), self.roi_bid + self.bid_step)

    def charge(self, price, cur_round, estimated_CTR, vlaue):
        self.spending += price
        self.step_spending += price
        if cur_round % self.rounds_per_step == 0:
            self.bid2spend_history.append([self.roi_bid, self.step_spending])
            self.roi_bid = self.calc_roi_bid(self.target_step_spending)
            self.step_spending = 0
            self.bid2spend_history = self.bid2spend_history[-self.memory:]

    def bid(self, value, context, estimated_CTR):
        if self.spending < self.budget:
            return value * estimated_CTR * self.roi_bid
        return 0

    def update(self, contexts, values, bids, prices, outcomes, estimated_CTRs, won_mask, iteration, plot, figsize, fontsize, name):
        self.spending = 0

    def reset(self):
        self.bid2spend_history = []
        self.spending = 0
        self.roi_bid = 1.0


class BidCapBidder(IMPCBudgetBidder):
    """ Bid cap bidder """
    def charge(self, price, cur_round, estimated_CTR, value):
        super(BidCapBidder, self).charge(price, cur_round, estimated_CTR, value)
        self.roi_bid = np.minimum(self.roi_bid, 1.0)


def spend2value(spend, a, b):
    return (np.sqrt(b * b + 2 * a * spend) - b) / a


def opt_spend(a, b):
    return (2.0 - 2.0 * b) / a


def fitf(func, args, bounds, datax, datay):
    ret = optimize.curve_fit(func, datax, datay, args, bounds=bounds, maxfev=1000)
    args, _ = ret
    return args


def fit_model(spend, value):
    args = [1, 1]
    bounds = ([0, 0], [np.inf, np.inf])
    try:
        args = fitf(spend2value, args, bounds, spend, value)
        return True, args[0], args[1]
    except Exception as e:
        print('spb fit error:', e)
        return False, 1, 1


class SPBBidder(IMPCBudgetBidder):
    """ spb bidder """
    def __init__(self, rng, budget_per_iter_range, rounds_per_iter, rounds_per_step, bid_step, memory, spb_memory, explore_bid_max):
        super(SPBBidder, self).__init__(rng, budget_per_iter_range, rounds_per_iter, rounds_per_step, bid_step, memory)
        self.spb_memory = spb_memory
        self.explore_bid_max = explore_bid_max
        self.optimal_budget = -1
        self.spend_history = []
        self.value_history = []

    def charge(self, price, cur_round, estimated_CTR, value):
        self.spending += price
        self.step_spending += price
        if cur_round % self.rounds_per_step == 0:
            self.bid2spend_history.append([self.roi_bid, self.step_spending])
            if self.optimal_budget > 0:
                target_step_spending = self.optimal_budget * self.rounds_per_step / self.rounds_per_iter
                self.roi_bid = self.calc_roi_bid(target_step_spending)
            else:
                step_spending = self.budget * self.rounds_per_step / self.rounds_per_iter
                bid = self.calc_roi_bid(step_spending)
                self.roi_bid = np.minimum(bid, self.explore_bid_max)
            self.step_spending = 0
            self.bid2spend_history = self.bid2spend_history[-self.memory:]


    def update(self, contexts, values, bids, prices, sum_values, estimated_CTRs, won_mask, iteration, plot, figsize, fontsize, name):
        if contexts is not None:
            self.spend_history.append(np.sum(prices[won_mask]))
            self.spend_history=self.spend_history[-self.spb_memory:]
            self.value_history.append(sum_values)
            self.value_history=self.value_history[-self.spb_memory:]
        model_ready, a, b = False, 1, 1
        if len(self.spend_history) > 1:
            model_ready, a, b = fit_model(self.spend_history, self.value_history)
        #print("spb model: ", model_ready, a, b, self.optimal_budget, self.spend_history, self.value_history)
        if model_ready:
            self.optimal_budget = np.minimum(opt_spend(a, b), self.budget)
        else:
            self.optimal_budget = -1
        self.spending = 0

    def reset(self):
        super(SPBBidder, self).reset()
        self.optimal_budget = -1
        self.spend_history = []
        self.value_history = []


class MPCBidder(BudgetRistrictedBidder):
    def __init__(self, rng, budget_per_iter_range, rounds_per_iter, rounds_per_step, bid_step, memory, kp, ki, kd, bid_min, bid_max):
        super(MPCBidder, self).__init__(rng, budget_per_iter_range, rounds_per_iter)
        self.rounds_per_step = rounds_per_step
        self.rounds_per_iter = rounds_per_iter
        self.bid_step = bid_step
        self.roi_bid = 1.0
        self.prediction_diff = 1.0
        self.estimated_value = 0
        self.memory = memory
        self.value_history = []
        self.estimated_value_history = []
        self.bid_min = bid_min
        self.bid_max = bid_max

        self.kp = kp
        self.ki = ki
        self.kd = kd

        self.error_p = 0
        self.last_error_p = 0
        self.error_i = 0
        self.error_d = 0

    def charge(self, price, cur_round, estimated_CTR, value):
        if price > 0 :
            self.spending += price
            self.estimated_value += estimated_CTR * value

        # adjust roi_bid (pid with roi)
        if cur_round % self.rounds_per_step == 0:
            roi_target = 1.0
            value = self.estimated_value * self.prediction_diff
            if self.spending > 0 and value > 0:
                value = self.estimated_value * self.prediction_diff
                roi = value / self.spending
                self.error_p = roi - roi_target
                self.error_i += self.error_p
                self.error_d = self.error_p - self.last_error_p
                self.last_error_p = self.error_p

                control = self.kp * self.error_p + self.ki * self.error_i + self.kd * self.error_d

                bid = self.roi_bid + control
                self.roi_bid = np.minimum(np.maximum(bid, self.roi_bid - self.bid_step), self.roi_bid + self.bid_step)
                self.roi_bid = np.minimum(np.maximum(self.roi_bid, self.bid_min), self.bid_max)
            else:
                self.roi_bid = np.minimum(self.bid_max, self.roi_bid + self.bid_step)

    def bid(self, value, context, estimated_CTR):
        if self.spending < self.budget:
            return value * estimated_CTR * self.roi_bid
        return 0

    def update(self, contexts, values, bids, prices, sum_values, estimated_CTRs, won_mask, iteration, plot, figsize, fontsize, name):
        self.spending = 0
        self.estimated_value = 0
        if contexts is not None:
            self.estimated_value_history.append(np.sum(np.multiply(estimated_CTRs, values)[won_mask]))
            self.estimated_value_history=self.estimated_value_history[-self.memory:]
            self.value_history.append(sum_values)
            self.value_history=self.value_history[-self.memory:]
            value = sum(self.value_history)
            estimated_value = sum(self.estimated_value_history)
            if value > 1 and estimated_value > 1:
                self.prediction_diff = value / estimated_value
            else:
                self.prediction_diff = 1.0
            #print(f"bid={self.roi_bid} diff={self.prediction_diff} e={estimated_value} v={value}")

    #def reset(self):

        #self.estimated_value = 0
        #self.roi_bid = 1.0

        #self.error_p = 0
        #self.last_error_p = 0
        #self.error_i = 0
        #self.error_d = 0

