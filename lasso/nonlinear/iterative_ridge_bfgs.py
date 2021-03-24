import warnings
from scipy.optimize import OptimizeResult
from scipy.optimize.optimize import _status_message
from torch import Tensor
import torch
import torch.autograd as autograd
from torch.optim.lbfgs import _strong_wolfe

from ..linear.utils import batch_cholesky_solve

Inf = float('inf')


def masked_scatter(trg, mask, src):
    return trg.masked_scatter(mask, src.masked_select(mask))


def pinv(x, eps=1e-8):
    return x.reciprocal().masked_fill(x < eps, 0)


@torch.no_grad()
def iterative_ridge_bfgs(f, x0, alpha=1.0, gtol=1e-5, lr=1.0,
                         line_search=True, normp=Inf, maxiter=None,
                         return_losses=False, disp=False):
    """A BFGS analogue to Iterative Ridge for nonlinear reconstruction terms.

    Parameters
    ----------
    f : callable
        Scalar objective function to minimize
    x0 : Tensor
        Initialization point
    gtol : float
        Gradient norm must be less than `gtol` before successful
        termination.
    lr : float
        Initial step size (learning rate) for each line search.
    line_search : bool
        Whether or not to perform line search for optimal step size.
    normp : int, inf
        The norm type to use for calculating gradient magnitude.
    maxiter : int, optional
        Maximum number of iterations to perform. Defaults to 200 * num_params
    disp : bool
        Set to True to print convergence messages.
    """
    assert x0.dim() == 2
    xshape = x0.shape
    x = x0.detach()
    if maxiter is None:
        maxiter = x.size(1) * 200


    def terminate(warnflag, msg):
        if disp:
            print(msg)
            print("         Current function value: %f" % fval)
            print("         Iterations: %d" % k)
            print("         Function evaluations: %d" % nfev)
        result = OptimizeResult(fun=fval, jac=grad, nfev=nfev,
                                status=warnflag, success=(warnflag==0),
                                message=msg, x=x, nit=k)
        if return_losses:
            return result, losses
        return result

    def evaluate(x):
        x = x.detach().requires_grad_(True)
        with torch.enable_grad():
            fval = f(x)
        grad, = autograd.grad(fval, x)
        return fval.detach(), grad

    def dir_evaluate(x, t, d):
        """used for strong-wolfe line search"""
        x = (x + t * d).reshape(xshape)
        fval, grad = evaluate(x)
        return fval, grad.flatten()

    outer = lambda u,v: u.unsqueeze(-1) * v.unsqueeze(-2)
    inner = lambda u,v: torch.sum(u*v, 1, keepdim=True)

    # compute initial f(x) and f'(x)
    fval, grad = evaluate(x)
    nfev = 1
    if disp > 1:
        print('initial loss: %0.4f' % fval)
    if return_losses:
        losses = [fval.item()]

    # initialize BFGS
    H = torch.diag_embed(torch.ones_like(x))  # [B,D,D]

    # BFGS iterations
    for k in range(1, maxiter + 1):
        # set the initial step size
        if k == 1:
            # use sample-specific learning rate for the first step,
            # unless we're doing a line search.
            t = (lr / grad.abs().sum(1, keepdim=True)).clamp(None, lr)
            if line_search:
                t = t.mean().item()
        else:
            t = lr

        # compute newton direction
        if k == 1:
            d = grad.neg()
            if alpha > 0:
                d -= alpha * x.sign()
        else:
            Hk = H
            if alpha > 0:
                Hk = Hk + torch.diag_embed(2 * alpha * pinv(x.abs()))
            d = batch_cholesky_solve(grad.neg(), Hk)

        # update variables (with optional strong-wolfe line search)
        if line_search:
            gtd = torch.sum(grad * d)
            fval, grad_new, t, ls_nevals = \
                _strong_wolfe(dir_evaluate, x.flatten(), t, d.flatten(), fval,
                              grad.flatten(), gtd)
            x_new = x + t * d
            grad_new = grad_new.reshape(xshape)
            nfev += ls_nevals
        else:
            x_new = x + t * d
            fval, grad_new = evaluate(x_new)
            nfev += 1

        if disp > 1:
            print('iter %3d - loss: %0.4f' % (k, fval))
        if return_losses:
            losses.append(fval.item())

        # update \delta x and \delta f'(x)
        s = x_new - x
        y = grad_new - grad
        x = x_new
        grad = grad_new

        # stopping check
        grad_norm = grad.norm(normp)
        if grad_norm <= gtol:
            return terminate(0, _status_message['success'])
        if fval.isinf() or fval.isnan():
            return terminate(2, _status_message['pr_loss'])

        # update the BFGS hessian approximation
        rho_inv = inner(y, s)
        valid = torch.any(rho_inv.abs() > 1e-10, 1, keepdim=True)
        if not valid.all():
            warnings.warn("Divide-by-zero encountered: rho assumed large")
        rho = torch.where(valid,
                          rho_inv.reciprocal(),
                          torch.full_like(rho_inv, 1000.))

        HssH = torch.bmm(H, torch.bmm(outer(s, s), H.transpose(-1,-2)))
        sHs = inner(s, torch.bmm(H, s.unsqueeze(-1)).squeeze(-1))
        H = masked_scatter(H,
                           valid.unsqueeze(-1),
                           H + rho.unsqueeze(-1) * outer(y, y) - HssH / sHs.unsqueeze(-1))

    # final sanity check
    if grad_norm.isnan() or fval.isnan() or x.isnan().any():
        return terminate(3, _status_message['nan'])

    # if we get to the end, the maximum num. iterations was reached
    return terminate(1, "Warning: " + _status_message['maxiter'])