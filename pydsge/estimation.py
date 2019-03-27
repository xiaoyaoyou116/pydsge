#!/bin/python2
# -*- coding: utf-8 -*-
import numpy as np
import warnings
import os
import time
import emcee
from .stats import InvGammaDynare, summary, mc_mean, inv_gamma_spec
import pathos
import scipy.stats as ss
import scipy.optimize as so
import tqdm
from .plots import traceplot, posteriorplot


def mcmc(p0, linear_mcmc, nwalkers, ndim, ndraws, priors, sampler, ntemp, ncores, update_freq, description, verbose):
    # very very dirty hack

    import tqdm
    import pathos

    # globals are *evil*
    global lprob_global
    global llike_global
    global lprior_global

    # import the global function and hack it to pretend it is defined on the top level
    def lprob_local(par):
        return lprob_global(par, linear_mcmc)

    def llike_local(par):
        return llike_global(par, linear_mcmc)

    def lprior_local(par):
        return lprior_global(par)

    # all that should be reproducible
    np.random.seed(0)

    loc_pool = pathos.pools.ProcessPool(ncores)

    if sampler is 'ptes':
        sampler = emcee.PTSampler(ntemps=ntemp, nwalkers=nwalkers,
                                  dim=ndim, logp=lprior_local, logl=llike_local, pool=loc_pool)
    else:
        sampler = emcee.EnsembleSampler(
            nwalkers, ndim, lprob_local, pool=loc_pool)

    if not verbose:
        np.warnings.filterwarnings('ignore')
        pbar = tqdm.tqdm(total=ndraws, unit='sample(s)', dynamic_ncols=True)
        report = pbar.write
    else:
        report = print

    cnt = 0
    for result in sampler.sample(p0, iterations=ndraws):
        if update_freq and cnt and not cnt % update_freq:
            report('')
            if description is not None:
                report('[bayesian_estimation -> mcmc:]'.ljust(45, ' ') +
                       ' Summary from last %s of %s iterations (%s):' % (update_freq, cnt, str(description)))
            else:
                report('[bayesian_estimation -> mcmc:]'.ljust(45, ' ') +
                       ' Summary from last %s of %s iterations:' % (update_freq, cnt))
            report(str(summary(sampler.chain.reshape(-1, ndraws, ndim)
                               [:, cnt-update_freq:cnt, :], priors).round(3)))
            report("Mean acceptance fraction: {0:.3f}".format(
                np.mean(sampler.acceptance_fraction)))
        if not verbose:
            pbar.update(1)
        cnt += 1

    loc_pool.close()
    loc_pool.join()
    loc_pool.clear()

    if not verbose:
        np.warnings.filterwarnings('default')

    pbar.close()

    return sampler


def bayesian_estimation(self, N=300, linear=False, ndraws=3000, tune=None, ncores=None, nwalkers=100, ntemp=4, maxfev=None, linear_pre_pmdm=False, pmdm_method=None, pmdm_tol=1e-2, sampler=None, update_freq=None, verbose=False):

    if ncores is None:
        ncores = pathos.multiprocessing.cpu_count()

    if tune is None:
        tune = int(ndraws*4/5.)

    if update_freq is None:
        update_freq = int(ndraws/4.)

    if hasattr(self, 'description'):
        description = self.description
    else:
        description = None

    if maxfev is None:
        maxfev = ndraws

    self.preprocess(verbose=verbose > 1)

    # dry run before the fun beginns
    self.create_filter(N=N, linear=linear_pre_pmdm or linear)
    self.get_ll(verbose=verbose)

    print()
    print('[bayesian_estimation:]'.ljust(30, ' ') +
          ' Model operational. %s states, %s observables. Ready for estimation.' % (len(self.vv), len(self.observables)))
    print()

    par_fix = np.array(self.par).copy()

    p_names = [p.name for p in self.parameters]
    priors = self['__data__']['estimation']['prior']
    prior_arg = [p_names.index(pp) for pp in priors.keys()]

    # add to class so that it can be stored later
    self.par_fix = par_fix
    self.prior_arg = prior_arg
    self.ndraws = ndraws

    init_par = par_fix[prior_arg]

    ndim = len(priors.keys())

    print('[bayesian_estimation:]'.ljust(30, ' ') +
          ' %s priors detected. Adding parameters to the prior distribution.' % ndim)

    priors_lst = []
    for pp in priors:
        dist = priors[str(pp)]
        pmean = dist[1]
        pstdd = dist[2]
        # simply make use of frozen distributions
        if str(dist[0]) == 'uniform':
            priors_lst.append(ss.uniform(loc=pmean, scale=pstdd-pmean))

        elif str(dist[0]) == 'normal':
            priors_lst.append(ss.norm(loc=pmean, scale=pstdd))
        elif str(dist[0]) == 'gamma':
            b = pstdd**2/pmean
            a = pmean/b
            priors_lst.append(ss.gamma(a, scale=b))
        elif str(dist[0]) == 'beta':
            a = (1-pmean)*pmean**2/pstdd**2 - pmean
            b = a*(1/pmean - 1)
            priors_lst.append(ss.beta(a=a, b=b))
        elif str(dist[0]) == 'inv_gamma':

            def targf(x):
                y0 = ss.invgamma(x[0], loc=x[1]).std() - pstdd
                y1 = ss.invgamma(x[0], loc=x[1]).mean() - pmean
                return np.array([y0, y1])

            ig_res = so.root(targf, np.array([4, pmean]))
            if ig_res['success']:
                a = ig_res['x']
                priors_lst.append(ss.invgamma(a[0], loc=a[1]))
            else:
                raise ValueError(
                    'Can not find inverse gamma distribution with mean %s and std %s' % (pmean, pstdd))
        elif str(dist[0]) == 'inv_gamma_dynare':
            s, nu = inv_gamma_spec(pmean, pstdd)
            ig = InvGammaDynare()
            ig.pars(nu, s)
            priors_lst.append(ig)

        else:
            raise NotImplementedError(
                ' Distribution *not* implemented: ', str(dist[0]))
        print('     parameter %s as %s with mean %s and std/df %s...' %
              (pp, dist[0], pmean, pstdd))

    def llike(parameters, linear_llike):

        if verbose == 2:
            st = time.time()

        with warnings.catch_warnings(record=True):
            try:
                warnings.filterwarnings('error')

                par_fix[prior_arg] = parameters
                par_active_lst = list(par_fix)

                self.get_sys(par=par_active_lst,
                             reduce_sys=True, verbose=verbose > 1)

                # these max vals should be sufficient given we're only dealing with stochastic linearization
                if not linear_llike:
                    self.preprocess(l_max=3, k_max=16, verbose=verbose > 1)
                else:
                    self.preprocess(l_max=1, k_max=0, verbose=False)

                self.create_filter(N=N, linear=linear_llike)

                ll = self.get_ll(verbose=verbose)

                if verbose == 2:
                    print('[bayesian_estimation -> llike:]'.ljust(45, ' ') +
                          ' Sample took '+str(np.round(time.time() - st, 3))+'s.')

                return ll

            except KeyboardInterrupt:
                raise

            except:
                if verbose == 2:
                    print('[bayesian_estimation -> llike:]'.ljust(45, ' ') +
                          ' Sample took '+str(np.round(time.time() - st, 3))+'s. (failure)')

                return -np.inf

    def lprior(pars):

        prior = 0
        for i in range(len(priors_lst)):
            prior += priors_lst[i].logpdf(pars[i])

        return prior

    def lprob(pars, linear_lprob):
        return lprior(pars) + llike(pars, linear_lprob)

    global lprob_global
    global llike_global
    global lprior_global

    lprob_global = lprob
    llike_global = llike
    lprior_global = lprior
    prior_names = [pp for pp in priors.keys()]

    class pmdm(object):
        # thats a wrapper to have a progress par in the posterior maximization

        name = 'pmdm'

        def __init__(self, init_par, method, linear_pmdm):

            self.n = 0
            self.maxfev = maxfev
            if not verbose:
                self.pbar = tqdm.tqdm(total=maxfev, dynamic_ncols=True)
            self.init_par = init_par
            self.st = 0
            self.update_ival = 1
            self.timer = 0
            self.res_max = np.inf
            self.method = method
            self.linear = linear_pmdm
            if linear_pmdm:
                self.desc_str = 'linear_'
            else:
                self.desc_str = ''

        def __call__(self, pars):

            self.res = -lprob(pars, self.linear)
            self.x = pars

            # better ensure we're not just running with the wolfs when maxfev is hit
            if self.res < self.res_max:
                self.res_max = self.res
                self.x_max = self.x

            self.n += 1
            self.timer += 1

            if not verbose and self.timer == self.update_ival:

                # ensure displayed number is correct
                self.pbar.n = self.n
                self.pbar.update(0)

                difft = time.time() - self.st
                if difft < 1:
                    self.update_ival *= 2
                if difft > 2 and self.update_ival > 1:
                    self.update_ival /= 2

                self.pbar.set_description(
                    'll: '+str(-self.res.round(5)).rjust(12, ' ')+' ['+str(-self.res_max.round(5))+']')
                self.st = time.time()
                self.timer = 0

            if not verbose:
                report = self.pbar.write
            else:
                report = print

            # prints information snapshots
            if update_freq and not self.n % update_freq:
                # getting the number of colums isn't that easy
                with os.popen('stty size', 'r') as rows_cols:
                    cols = rows_cols.read().split()[1]
                if description is not None:
                    report('[bayesian_estimation -> '+self.desc_str+'pmdm:]'.ljust(
                        45, ' ')+' Current best guess @ iteration %s and ll of %s (%s):' % (self.n, self.res_max.round(5), str(description)))
                else:
                    report('[bayesian_estimation -> '+self.desc_str+'pmdm:]'.ljust(
                        45, ' ')+' Current best guess @ iteration %s and ll of %s):' % (self.n, self.res_max.round(5)))
                # split the info such that it is readable
                lnum = (len(priors)*8)//(int(cols)-8) + 1
                priors_chunks = np.array_split(np.array(prior_names), lnum)
                vals_chunks = np.array_split(
                    [round(m_val, 3) for m_val in self.x_max], lnum)
                for pchunk, vchunk in zip(priors_chunks, vals_chunks):
                    row_format = "{:>8}" * (len(pchunk) + 1)
                    report(row_format.format("", *pchunk))
                    report(row_format.format("", *vchunk))
                    report('')
                report('')

            if self.n >= maxfev:
                raise StopIteration

            return self.res

        def go(self):

            try:
                f_val = -np.inf
                self.x = self.init_par

                res = so.minimize(self, self.x, method=self.method,
                                  tol=pmdm_tol, options=opt_dict)

                if not verbose:
                    self.pbar.close()
                print('')
                if self.res_max < res['fun']:
                    print('[bayesian_estimation -> '+self.desc_str+'pmdm:]'.ljust(45, ' ')+str(res['message']) +
                          ' Maximization returned value lower than actual (known) optimum ('+str(-self.res_max)+' > '+str(-self.res)+').')
                else:
                    print('[bayesian_estimation -> '+self.desc_str+'pmdm:]'.ljust(45, ' ')+str(res['message']
                                                                                               )+' Log-likelihood is '+str(np.round(-res['fun'], 5))+'.')
                print('')

            except StopIteration:
                if not verbose:
                    self.pbar.close()
                print('')
                print('[bayesian_estimation -> '+self.desc_str+'pmdm:]'.ljust(45, ' ') +
                      ' Maximum number of function calls exceeded, exiting. Log-likelihood is '+str(np.round(-self.res_max, 5))+'...')
                print('')

            except KeyboardInterrupt:
                if not verbose:
                    self.pbar.close()
                print('')
                print('[bayesian_estimation -> '+self.desc_str+'pmdm:]'.ljust(45, ' ') +
                      ' Iteration interrupted manually. Log-likelihood is '+str(np.round(-self.res_max, 5))+'...')
                print('')

            return self.x_max

    if maxfev:

        print()
        opt_dict = {}
        if pmdm_method is None:
            pmdm_method = 'Nelder-Mead'
        elif isinstance(pmdm_method, int):
            methodl = ["Nelder-Mead", "Powell", "BFGS", "CG",
                       "L-BFGS-G", "SLSQP", "trust-constr", "COBYLA", "TNC"]

            # Nelder-Mead: fast and reliable, but doesn't max out the likelihood completely (not that fast if far away from max)
            # Powell: provides the highes likelihood but is slow and sometimes ends up in strange corners of the parameter space (sorting effects)
            # BFGS: hit and go but *can* outperform Nelder-Mead without sorting effects
            # CG: *can* perform well but can also get lost in a bad region with low LL
            # L-BFGS-G: leaves values untouched
            # SLSQP: fast but not very precise (or just wrong)
            # trust-constr: very fast but terminates too early
            # COBYLA: very fast but hangs up for no good reason and is effectively unusable
            # TNC: gets stuck around the initial values

            pmdm_method = methodl[pmdm_method]
            print('[bayesian_estimation -> pmdm:]'.ljust(45, ' ') +
                  ' Available methods are %s.' % ', '.join(methodl))
        if pmdm_method == 'trust-constr':
            opt_dict = {'maxiter': np.inf}
        if pmdm_method == 'Nelder-Mead':
            opt_dict = {
                'maxiter': np.inf,
                'maxfev': np.inf
            }
        if not verbose:
            np.warnings.filterwarnings('ignore')
            print('[bayesian_estimation -> pmdm:]'.ljust(45, ' ') +
                  " Maximizing posterior mode density using '%s' (meanwhile warnings are disabled)." % pmdm_method)
        else:
            print('[bayesian_estimation -> pmdm:]'.ljust(45, ' ') +
                  ' Maximizing posterior mode density using %s.' % pmdm_method)
        print()

        if linear_pre_pmdm:
            print('[bayesian_estimation -> pmdm:]'.ljust(45, ' ') +
                  ' Starting pre-maximization of linear function.')
            init_par = pmdm(init_par, pmdm_method, linear_pmdm=True).go()
            print('[bayesian_estimation -> pmdm:]'.ljust(45, ' ') +
                  ' Pre-maximization of linear function done, starting actual maximization.')
        result = pmdm(init_par, pmdm_method, linear_pmdm=linear).go()
        np.warnings.filterwarnings('default')
        init_par = result

    print()
    print('[bayesian_estimation:]'.ljust(30, ' ')+' Inital values for MCMC:')
    with os.popen('stty size', 'r') as rows_cols:
        cols = rows_cols.read().split()[1]
        lnum = (len(priors)*8)//(int(cols)-8) + 1
        priors_chunks = np.array_split(np.array(prior_names), lnum)
        vals_chunks = np.array_split([round(m_val, 3)
                                      for m_val in init_par], lnum)
        for pchunk, vchunk in zip(priors_chunks, vals_chunks):
            row_format = "{:>8}" * (len(pchunk) + 1)
            print(row_format.format("", *pchunk))
            print(row_format.format("", *vchunk))
            print()

    if ndraws:
        print()
        if sampler == 'ptes':
            pos = init_par*(1+1e-3*np.random.randn(ntemp, nwalkers, ndim))
        else:
            pos = [init_par*(1+1e-3*np.random.randn(ndim))
                   for i in range(nwalkers)]
        sampler = mcmc(pos, linear, nwalkers, ndim, ndraws, priors, sampler,
                       ntemp, ncores, update_freq, description, verbose)

        print("Mean acceptance fraction: {0:.3f}"
              .format(np.mean(sampler.acceptance_fraction)))

        self.chain = sampler.chain.reshape(-1, ndraws, ndim)

        sampler.summary = lambda: summary(self.chain[:, tune:, :], priors)
        sampler.traceplot = lambda **args: traceplot(
            self.chain, varnames=prior_names, tune=tune, priors=priors_lst, **args)
        sampler.posteriorplot = lambda **args: posteriorplot(
            self.chain, varnames=prior_names, tune=tune, **args)

        par_mean = par_fix
        par_mean[prior_arg] = mc_mean(self.chain[:, tune:], varnames=priors)

        sampler.prior_dist = priors_lst
        sampler.prior_names = prior_names
        sampler.tune = tune
        sampler.par_means = list(par_mean)

        self.sampler = sampler
