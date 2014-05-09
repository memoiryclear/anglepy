import numpy as np
import scipy
import scipy.optimize
import anglepy.ndict as ndict
import anglepy.BNModel as BNModel
import hmc
import time

# Training loop for MCEM
def loop_mcem(dostep, w, hook, hook_wavelength=2, n_iters=9999999):
	t_prev = time.time()
	
	z = [[]]
	logpxz = [[]]
	
	def getLoglik():
		if len(z[0]) < 4: return np.zeros(((0))), np.zeros(((0)))
		
		_z, _logpxz = hmc.combine_samples(z[0], logpxz[0])
		ll, ll_var = hmc.estimate_mcmc_likelihood(_z, _logpxz, len(z[0]))
		z[0] = []
		logpxz[0] = []
		return ll, ll_var
	
	for t in xrange(1, n_iters):
		_z, _logpxz = dostep(w)
		z[0].append(_z)
		logpxz[0].append(_logpxz)
		if t == 1 or time.time() - t_prev > hook_wavelength:
			ll, ll_var = getLoglik()
			hook(t, w, _z, ll, ll_var)
			t_prev = time.time()
	
	ll, ll_var = getLoglik()
	hook(n_iters-1, w, _z, ll, ll_var)
	
	print 'Optimization loop finished'

def lbfgs_wz(model, w, z, x, hook=None, maxiter=None):
	
	def f(y):
		_w, _z = ndict.unflatten_multiple(y, [w, z])
		logpx, logpz = model.logpxz(_w, x, _z)
		return - (logpx.sum() + logpz.sum())
	
	def fprime(y):
		_w, _z = ndict.unflatten_multiple(y, [w, z])
		logpx, logpz, gw, gz = model.dlogpxz_dwz(_w, x, _z)
		gwz = ndict.flatten_multiple((gw, gz))
		return - gwz
	
	t = [0, 0, time.time()]
	def callback(wz):
		if hook is None: return
		_w, _z = ndict.unflatten_multiple(wz, (w, z))
		t[1] += 1
		hook(t[1], _w, _z)
	
	x0 = ndict.flatten_multiple((w, z))
	xn, f, d = scipy.optimize.fmin_l_bfgs_b(func=f, x0=x0, fprime=fprime, m=100, iprint=0, callback=callback, maxiter=maxiter)
	
	#scipy.optimize.fmin_cg(f=f, x0=x0, fprime=fprime, full_output=True, callback=hook)
	#scipy.optimize.fmin_ncg(f=f, x0=x0, fprime=fprime, full_output=True, callback=hook)
	w, z = ndict.unflatten_multiple(xn, (w, z))
	if d['warnflag'] is 2:
		print 'warnflag:', d['warnflag']
		print d['task']
	return w, z
	

def step_batch_mcem(model_p, x, z_mcmc, dostep_m, hmc_stepsize=1e-2, hmc_steps=20, m_steps=5):
	print 'Batch MCEM', hmc_stepsize, hmc_steps, m_steps
	
	n_batch = x.itervalues().next().shape[1]
	
	hmc_dostep = hmc.hmc_step_autotune(n_steps=hmc_steps, init_stepsize=hmc_stepsize)
	
	def doStep(w):
		
		def fgrad(_z):
			logpx, logpz, gw, gz = model_p.dlogpxz_dwz(w, x, _z)
			return logpx + logpz, gz
		
		# E-step
		logpxz, acceptRate, stepsize = hmc_dostep(fgrad, z_mcmc)

		# M-step
		for i in range(m_steps):
			#print 'm-step:', i
			dostep_m(w, z_mcmc)
		
		return z_mcmc.copy(), logpxz.copy() 
		
	return doStep

# HMC with both weights 'w' and latents 'z'
# Problem: stepsize of 'w' becomes really small
def step_hmc_wz(model, x, z, hmc_stepsize=1e-2, hmc_steps=20):
	print 'step_hmc_wz', hmc_stepsize, hmc_steps

	n_batch = x.itervalues().next().shape[1]
	
	hmc_dostep_z = hmc.hmc_step_autotune(n_steps=hmc_steps, init_stepsize=hmc_stepsize)
	hmc_dostep_w = hmc.hmc_step_autotune(n_steps=hmc_steps, init_stepsize=hmc_stepsize)
	
	def dostep(w):
		
		def fgrad_z(_z):
			logpx, logpz, gw, gz = model.dlogpxz_dwz(w, x, _z)
			return logpx + logpz, gz
		
		logpxz, acceptRate, stepsize = hmc_dostep_z(fgrad_z, z)

		shapes_w = ndict.getShapes(w)
		
		def vectorize(d):
			v = {}
			for i in d: v[i] = d[i].reshape((d[i].size, -1))
			return v
		
		def fgrad_w(_w):
			_w = ndict.setShapes(_w, shapes_w)
			logpx, logpz, gw, gz = model.dlogpxz_dwz(_w, x, z)
			gw = vectorize(gw)
			return logpx + logpz, gw
		
		_w = vectorize(w)
		hmc_dostep_w(fgrad_w, _w)
		
		return z.copy(), logpxz.copy() 
		
	return dostep

# Training loop for PVEM and Wake-Sleep algorithms
def loop_pvem(dostep, w, model, hook, hook_wavelength=2, n_iters=9999999):
	
	t_prev = time.time()
	logpxz = n = 0
	
	for t in xrange(1, n_iters):
		z, _logpxz = dostep(w)
		logpxz += _logpxz
		n += 1
		if t == 1 or t == n_iters-1 or time.time() - t_prev > hook_wavelength:
			logpxz /= n
			hook(t, w, z, logpxz)
			logpxz = 0
			n = 0
			t_prev = time.time()
	
	print 'Optimization loop finished'

# PVEM B (Predictive Variational EM)
def step_pvem(model_q, model_p, x, w_q, n_batch=100, ada_stepsize=1e-1, warmup=10, reg=1e-8, convertImgs=False):
	print 'Predictive VEM', ada_stepsize
	
	hmc_steps=0
	hmc_dostep = hmc.hmc_step_autotune(n_steps=hmc_steps, init_stepsize=1e-1)
	
	# We're using adagrad stepsizes
	gw_q_ss = ndict.cloneZeros(w_q)
	gw_p_ss = ndict.cloneZeros(model_p.init_w())
	
	nsteps = [0]
	
	do_adagrad = True
	
	def doStep(w_p):
		
		#def fgrad(_z):
		#	logpx, logpz, gw, gz = model_p.dlogpxz_dwz(w, x, _z)
		#	return logpx + logpz, gz
		n_tot = x.itervalues().next().shape[1]
		idx_minibatch = np.random.randint(0, n_tot, n_batch)
		x_minibatch = {i:x[i][:,idx_minibatch] for i in x}
		if convertImgs: x_minibatch = {i:x_minibatch[i]/256. for i in x_minibatch}
			
		# step 1A: sample z ~ p(z|x) from model_q
		_, z, _  = model_q.gen_xz(w_q, x_minibatch, {}, n_batch)
		
		# step 1B: update z using HMC
		def fgrad(_z):
			logpx, logpz, gw, gz = model_p.dlogpxz_dwz(w_p, _z, x_minibatch)
			return logpx + logpz, gz
		if (hmc_steps > 0):
			logpxz, _, _ = hmc_dostep(fgrad, z)

		def optimize(w, gw, gw_ss, stepsize):
			if do_adagrad:
				for i in gw:
					gw_ss[i] += gw[i]**2
					if nsteps[0] > warmup:
						w[i] += stepsize / np.sqrt(gw_ss[i]+reg) * gw[i]
					#print (stepsize / np.sqrt(gw_ss[i]+reg)).mean()
			else:
				for i in gw:
					w[i] += 1e-4 * gw[i]
		
		# step 2: use z to update model_p
		logpx_p, logpz_p, gw_p, gz_p = model_p.dlogpxz_dwz(w_p, x_minibatch, z)
		_, gw_prior = model_p.dlogpw_dw(w_p)
		gw = {i: gw_p[i] + float(n_batch)/n_tot * gw_prior[i] for i in gw_p}
		optimize(w_p, gw, gw_p_ss, ada_stepsize)
		
		# step 3: use gradients of model_p to update model_q
		_, logpz_q, fd, gw_q = model_q.dfd_dw(w_q, x_minibatch, z, gz_p)
		_, gw_prior = model_q.dlogpw_dw(w_q)
		gw = {i: -gw_q[i] + float(n_batch)/n_tot * gw_prior[i] for i in gw_q}
		optimize(w_q, gw, gw_q_ss, ada_stepsize)
		
		nsteps[0] += 1
		
		return z.copy(), logpx_p + logpz_p - logpz_q
		
	return doStep

# Wake-Sleep algorithm
def step_wakesleep(model_q, model_p, x, w_q, n_batch=100, ada_stepsize=1e-1, warmup=100, reg=1e-8, convertImgs=False):
	print 'Wake-Sleep', ada_stepsize
	
	# We're using adagrad stepsizes
	gw_q_ss = ndict.cloneZeros(w_q)
	gw_p_ss = ndict.cloneZeros(model_p.init_w())
	
	nsteps = [0]
	
	do_adagrad = True
	
	def doStep(w_p):
		
		n_tot = x.itervalues().next().shape[1]
		idx_minibatch = np.random.randint(0, n_tot, n_batch)
		x_minibatch = {i:x[i][:,idx_minibatch] for i in x}
		
		def optimize(w, gw, gw_ss, stepsize):
			if do_adagrad:
				for i in gw:
					gw_ss[i] += gw[i]**2
					if nsteps[0] > warmup:
						w[i] += stepsize / np.sqrt(gw_ss[i]+reg) * gw[i]
					#print (stepsize / np.sqrt(gw_ss[i]+reg)).mean()
			else:
				for i in gw:
					w[i] += 1e-4 * gw[i]
		
		# Wake phase: use z ~ q(z|x) to update model_p
		_, z, _  = model_q.gen_xz(w_q, x_minibatch, {}, n_batch)
		_, logpz_q = model_q.logpxz(w_q, x_minibatch, z)
		logpx_p, logpz_p, gw_p, gz_p = model_p.dlogpxz_dwz(w_p, x_minibatch, z)
		_, gw_prior = model_p.dlogpw_dw(w_p)
		gw = {i: gw_p[i] + float(n_batch)/n_tot * gw_prior[i] for i in gw_p}
		optimize(w_p, gw, gw_p_ss, ada_stepsize)
		
		# Sleep phase: use x ~ p(x|z) to update model_q
		x_p, z_p, _ = model_p.gen_xz(w_p, {}, {}, n_batch)
		_, _, gw_q, _ = model_q.dlogpxz_dwz(w_q, x_p, z_p)
		_, gw_prior = model_q.dlogpw_dw(w_q)
		gw = {i: gw_q[i] + float(n_batch)/n_tot * gw_prior[i] for i in gw_q}
		optimize(w_q, gw, gw_q_ss, ada_stepsize)
		
		nsteps[0] += 1
		
		return z.copy(), logpx_p + logpz_p - logpz_q
		
	return doStep

# Compute likelihood lower bound given prediction and generative model
# L is number of samples
def lowerbound_wakesleep(model_q, model_p, w_q, w_p, x, L=1, convertImgs=False):
	
	if convertImgs: x = {i:x[i]/256. for i in x}
	n_batch = x.itervalues().next().shape[1]
	
	logpxz = 0
	for _ in range(L):
		# Sample from q
		_, z, _  = model_q.gen_xz(w_q, x, {}, n_batch)
		# Measure the entropy term log q(z|x)
		_, logpz_q = model_q.logpxz(w_q, x, z)
		# Measure reconstruction error log p(x|z) and prior log p(z)
		logpx_p, logpz_p = model_p.logpxz(w_p, x, z)
		logpxz += logpx_p + logpz_p - logpz_q
	
	logpxz /= L
	
	return logpxz


# Training loop for variational autoencoder
def loop_va(dostep, w, hook, dt_hook=2, n_iters=9999999):
	
	t_prev = time.time()
	L = 0
	n = 0
	
	for t in xrange(1, n_iters):
		z, _L = dostep(w)
		L += _L.mean()
		n += 1
		if t == 1 or t == n_iters-1 or time.time() - t_prev > dt_hook:
			L /= n
			hook(t, w, z, L)
			L = 0
			n = 0
			t_prev = time.time()
	
	print 'Optimization loop finished'

# SGVB
def step_va(model, x, w, n_batch=100, stepsize=1e-1, warmup=100, anneal=True, convertImgs=False):
	print 'Variational Auto-Encoder', n_batch, stepsize, warmup
	
	# We're using adagrad stepsizes
	gw_ss = ndict.cloneZeros(w)
	
	nsteps = [0]
	
	def doStep(w):
		
		n_tot = x.itervalues().next().shape[1]
		idx_minibatch = np.random.randint(0, n_tot, n_batch)
		x_minibatch = {i:x[i][:,idx_minibatch] for i in x}
		if convertImgs: x_minibatch = {i:x_minibatch[i]/256. for i in x_minibatch}
		
		# Sample epsilon from prior
		z = model.gen_eps(n_batch)
		#for i in z: z[i] *= 0
		
		# Get gradient
		logpx, logpz, logqz, gw = model.dL_dw(w, x_minibatch, z)		
		_, gw_prior = model.dlogpw_dw(w)
		gw = {i: gw[i] + float(n_batch)/n_tot * gw_prior[i] for i in gw}
		
		# Update parameters
		adagrad_reg = 1e-8
		c = 1
		if not anneal: c /= nsteps[0]+1
		for i in gw:
			gw_ss[i] += gw[i]**2
			if nsteps[0] > warmup:
				w[i] += stepsize / np.sqrt(gw_ss[i] * c + adagrad_reg) * gw[i]
		
		nsteps[0] += 1
		
		return z.copy(), logpx + logpz - logqz
		
	return doStep

# Compute likelihood lower bound given a variational auto-encoder
# L is number of samples
def est_loglik_va(model, w, x, L=1, convertImgs=False):
	
	if convertImgs: x = {i:x[i]/256. for i in x}
	n_batch = x.itervalues().next().shape[1]
	
	px = 0 # estimate of marginal likelihood
	lowbound = 0 # estimate of lower bound of marginal likelihood
	for _ in range(L):
		# Sample from eps
		z = model.gen_eps(n_batch)
		logpx, logpz, logqz = model.L(w, x, z)		
		lowbound += (logpx + logpz - logqz).mean()
		px += np.exp(logpx + logpz - logqz)
	
	lowbound /= L
	logpx = np.log(px / L).mean()
	return lowbound, logpx

# Naive SVB algorithm
# NOTE: Does NOT use prior on variational parameters
def step_naivesvb(model_q, model_p, x, w_q, n_batch=100, ada_stepsize=1e-1, warmup=100, reg=1e-8, convertImgs=False):
	print 'Naive SV Est', ada_stepsize
	
	# We're using adagrad stepsizes
	gw_q_ss = ndict.cloneZeros(w_q)
	gw_p_ss = ndict.cloneZeros(model_p.init_w())
	
	nsteps = [0]
	
	do_adagrad = True
	
	def doStep(w_p):
		
		n_tot = x.itervalues().next().shape[1]
		idx_minibatch = np.random.randint(0, n_tot, n_batch)
		x_minibatch = {i:x[i][:,idx_minibatch] for i in x}
		if convertImgs: x_minibatch = {i:x_minibatch[i]/256. for i in x_minibatch}
		
		def optimize(w, gw, gw_ss, stepsize):
			if do_adagrad:
				for i in gw:
					gw_ss[i] += gw[i]**2
					if nsteps[0] > warmup:
						w[i] += stepsize / np.sqrt(gw_ss[i]+reg) * gw[i]
					#print (stepsize / np.sqrt(gw_ss[i]+reg)).mean()
			else:
				for i in gw:
					w[i] += 1e-4 * gw[i]
		
		# Phase 1: use z ~ q(z|x) to update model_p
		_, z, _  = model_q.gen_xz(w_q, x_minibatch, {}, n_batch)
		_, logpz_q = model_q.logpxz(w_q, x_minibatch, z)
		logpx_p, logpz_p, gw_p, gz_p = model_p.dlogpxz_dwz(w_p, x_minibatch, z)
		_, gw_prior = model_p.dlogpw_dw(w_p)
		gw_p = {i: gw_p[i] + float(n_batch)/n_tot * gw_prior[i] for i in gw_p}
		
		# Phase 2: use x ~ p(x|z) to update model_q
		_, _, gw_q, _ = model_q.dlogpxz_dwz(w_q, x_minibatch, z)
		#_, gw_prior = model_q.dlogpw_dw(w_q)
		#gw_q = {i: gw_q[i] + float(n_batch)/n_tot * gw_prior[i] for i in gw_q}
		weight = np.sum(logpx_p) + np.sum(logpz_p) - np.sum(logpz_q) - float(n_batch)
		gw_q = {i: gw_q[i] * weight for i in gw_q}
		
		optimize(w_p, gw_p, gw_p_ss, ada_stepsize)
		optimize(w_q, gw_q, gw_q_ss, ada_stepsize)
		
		nsteps[0] += 1
		
		return z.copy(), logpx_p + logpz_p - logpz_q
		
	return doStep

# Black-box variational inference algorithm
def step_svb_blackbox(model_q, model_p, x, phi, n_batch=10, n_subbatch=10, ada_stepsize=1e-1, warmup=100, convertImgs=False):
	print 'Black-box variational inference', n_batch, ada_stepsize, 
	
	# We're using adagrad stepsizes
	gphi_ss = ndict.cloneZeros(phi)
	gw_ss = ndict.cloneZeros(model_p.init_w())
	
	# Control variate covariance and variance
	cv_cov = ndict.cloneZeros(phi)
	cv_var = ndict.cloneZeros(phi)
	cv_lr = 0.01
	
	nsteps = [0]
	
	def doStep(w):
		
		grad = ndict.cloneZeros(phi)
		gw = ndict.cloneZeros(w)

		for l in range(n_batch):
			n_tot = x.itervalues().next().shape[1]
			idx_minibatch = np.random.randint(0, n_tot, n_subbatch)
			x_minibatch = {i:x[i][:,idx_minibatch] for i in x}
			if convertImgs: x_minibatch = {i:x_minibatch[i]/256. for i in x_minibatch}
			
			# Use z ~ q(z|x) to compute d[LB]/d[gw]
			_, z, _  = model_q.gen_xz(phi, x_minibatch, {}, n_subbatch)
			_, logpz_q = model_q.logpxz(phi, x_minibatch, z)
			logpx_p, logpz_p, _gw, gz_p = model_p.dlogpxz_dwz(w, x_minibatch, z)
			for i in _gw: gw[i] += _gw[i]
			
			# Compute d[LB]/d[gphi]  where gphi = phi (variational params)
			_, _, gphi, _ = model_q.dlogpxz_dwz(phi, x_minibatch, z)
			weight = np.sum(logpx_p) + np.sum(logpz_p) - np.sum(logpz_q)
			
			for i in phi:
				f = gphi[i] * weight
				h = gphi[i]
				cv_cov[i] = cv_cov[i] + cv_lr * (f * h - cv_cov[i])
				cv_var[i] = cv_var[i] + cv_lr * (h**2 - cv_var[i])
				grad[i] += f - (cv_cov[i]/(cv_var[i] + 1e-8)) * h
		
		_, gwprior = model_p.dlogpw_dw(w)
		for i in gw: gw[i] += float(n_subbatch*n_batch)/n_tot * gwprior[i]

		def optimize(_w, _gw, gw_ss, stepsize):
			reg=1e-8
			for i in _gw:
				gw_ss[i] += _gw[i]**2
				if nsteps[0] > warmup:
					_w[i] += stepsize / np.sqrt(gw_ss[i]+reg) * _gw[i]

		optimize(w, gw, gw_ss, ada_stepsize)
		optimize(phi, grad, gphi_ss, ada_stepsize)
		
		nsteps[0] += 1
		
		if ndict.hasNaN(grad):
			raise Exception()
		if ndict.hasNaN(phi):
			raise Exception()
		
		return z.copy(), logpx_p + logpz_p - logpz_q
		
	return doStep

